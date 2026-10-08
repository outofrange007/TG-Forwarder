"""Persistence (SQLite): already processed messages, topic mapping, checkpoints.

Corresponds to ``uploaded.txt`` + ``topics.json`` of the original, but is
transaction-safe and thread-safe (dashboard thread + Telegram thread).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    source_chat  TEXT    NOT NULL,
    message_id   INTEGER NOT NULL,
    status       TEXT    NOT NULL,
    target_ids   TEXT,
    info         TEXT,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (source_chat, message_id)
);
CREATE TABLE IF NOT EXISTS topics (
    source_chat   TEXT    NOT NULL,
    source_topic  INTEGER NOT NULL,
    target_chat   TEXT    NOT NULL,
    target_topic  INTEGER NOT NULL,
    title         TEXT,
    PRIMARY KEY (source_chat, source_topic, target_chat)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- processed messages ---
    def is_processed(self, source_chat, message_id: int) -> bool:
        """True if the message was forwarded successfully or deliberately skipped."""
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM processed WHERE source_chat=? AND message_id=?",
                (str(source_chat), int(message_id)),
            ).fetchone()
        return bool(row) and row[0] in (STATUS_OK, STATUS_SKIPPED)

    def mark(self, source_chat, message_ids: Iterable[int], status: str,
             target_ids: Optional[Iterable[int]] = None, info: str = "") -> None:
        targets = ",".join(str(t) for t in (target_ids or []))
        rows = [(str(source_chat), int(mid), status, targets, info[:500], _now())
                for mid in message_ids]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO processed(source_chat, message_id, status, target_ids, info, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(source_chat, message_id) DO UPDATE SET "
                "status=excluded.status, target_ids=excluded.target_ids, "
                "info=excluded.info, updated_at=excluded.updated_at",
                rows,
            )
            self._conn.commit()

    def failed_ids(self, source_chat) -> List[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id FROM processed WHERE source_chat=? AND status=? ORDER BY message_id",
                (str(source_chat), STATUS_FAILED),
            ).fetchall()
        return [r[0] for r in rows]

    def counts(self, source_chat=None) -> dict:
        query = "SELECT status, COUNT(*) FROM processed"
        args = ()
        if source_chat is not None:
            query += " WHERE source_chat=?"
            args = (str(source_chat),)
        with self._lock:
            rows = self._conn.execute(query + " GROUP BY status", args).fetchall()
        result = {STATUS_OK: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
        result.update({k: v for k, v in rows})
        return result

    def recent(self, limit: int = 20) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_chat, message_id, status, target_ids, info, updated_at "
                "FROM processed ORDER BY updated_at DESC, message_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        keys = ("source_chat", "message_id", "status", "target_ids", "info", "updated_at")
        return [dict(zip(keys, r)) for r in rows]

    # --- Checkpoint (highest message processed without gaps) ---
    # Kept separately per source AND topic selection: a run over topic 5 only must
    # not advance the checkpoint for "all topics" (otherwise messages of other
    # topics would be skipped). Duplicates are prevented independently by the
    # ``processed`` table (key: source + message ID).
    @staticmethod
    def checkpoint_key(source_chat, topic_id: Optional[int] = None) -> str:
        key = f"checkpoint:{source_chat}"
        return f"{key}:topic:{int(topic_id)}" if topic_id else key

    def get_checkpoint(self, source_chat, topic_id: Optional[int] = None) -> int:
        value = self.get_value(self.checkpoint_key(source_chat, topic_id))
        return int(value) if value else 0

    def set_checkpoint(self, source_chat, message_id: int, topic_id: Optional[int] = None) -> None:
        if int(message_id) > self.get_checkpoint(source_chat, topic_id):
            self.set_value(self.checkpoint_key(source_chat, topic_id), str(int(message_id)))

    def get_value(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    # --- topic mapping ---
    def get_topic(self, source_chat, source_topic: int, target_chat) -> Optional[int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT target_topic FROM topics WHERE source_chat=? AND source_topic=? AND target_chat=?",
                (str(source_chat), int(source_topic), str(target_chat)),
            ).fetchone()
        return row[0] if row else None

    def get_topic_mapping(self, source_chat, source_topic: int, target_chat):
        """(target_topic, stored title) or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT target_topic, title FROM topics WHERE source_chat=? AND source_topic=? AND target_chat=?",
                (str(source_chat), int(source_topic), str(target_chat)),
            ).fetchone()
        return (row[0], row[1] or "") if row else None

    def set_topic(self, source_chat, source_topic: int, target_chat, target_topic: int,
                  title: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO topics(source_chat, source_topic, target_chat, target_topic, title) "
                "VALUES (?,?,?,?,?) ON CONFLICT(source_chat, source_topic, target_chat) "
                "DO UPDATE SET target_topic=excluded.target_topic, title=excluded.title",
                (str(source_chat), int(source_topic), str(target_chat), int(target_topic), title),
            )
            self._conn.commit()

    def topics(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_chat, source_topic, target_chat, target_topic, title FROM topics"
            ).fetchall()
        keys = ("source_chat", "source_topic", "target_chat", "target_topic", "title")
        return [dict(zip(keys, r)) for r in rows]

    # --- reset ---
    def reset(self, source_chat=None, include_topics: bool = False) -> None:
        with self._lock:
            if source_chat is None:
                self._conn.execute("DELETE FROM processed")
                self._conn.execute("DELETE FROM kv WHERE key LIKE 'checkpoint:%'")
                if include_topics:
                    self._conn.execute("DELETE FROM topics")
            else:
                self._conn.execute("DELETE FROM processed WHERE source_chat=?", (str(source_chat),))
                key = self.checkpoint_key(source_chat)
                self._conn.execute("DELETE FROM kv WHERE key=? OR key LIKE ?", (key, key + ":topic:%"))
                if include_topics:
                    self._conn.execute("DELETE FROM topics WHERE source_chat=?", (str(source_chat),))
            self._conn.commit()
