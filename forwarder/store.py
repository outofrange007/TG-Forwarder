"""PostgreSQL persistence with cross-instance claims and a one-time SQLite importer.

Stores already processed messages, the topic mapping and checkpoints.
All forwarder instances share the same database via the DATABASE_URL
environment variable, so they share one transfer history.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import psycopg


STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Schema is created with IF NOT EXISTS, so it is safe to run on every start.
_SCHEMA = (
    # Transfer history: one row per (source chat, message ID)
    """
    CREATE TABLE IF NOT EXISTS processed (
        source_chat TEXT NOT NULL,
        message_id BIGINT NOT NULL,
        status TEXT NOT NULL,
        target_ids TEXT,
        info TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_chat, message_id)
    )
    """,
    # Mapping of source topics to target topics
    """
    CREATE TABLE IF NOT EXISTS topics (
        source_chat TEXT NOT NULL,
        source_topic BIGINT NOT NULL,
        target_chat TEXT NOT NULL,
        target_topic BIGINT NOT NULL,
        title TEXT,
        PRIMARY KEY (source_chat, source_topic, target_chat)
    )
    """,
    # Generic key/value storage (used for checkpoints)
    """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # Short-lived reservations that prevent two instances from
    # transferring the same message at the same time
    """
    CREATE TABLE IF NOT EXISTS claims (
        source_chat TEXT NOT NULL,
        message_id BIGINT NOT NULL,
        owner TEXT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (source_chat, message_id)
    )
    """,
)


class _ClaimDenied(Exception):
    """Internal: raised to roll back the transaction when a claim is denied."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    # A claim left behind by a crashed instance expires after this many seconds.
    CLAIM_TTL_SECONDS = 3600

    def __init__(self, path):
        # Kept only for compatibility with existing Store(path) calls.
        # This path is NOT used as the runtime database anymore.
        self.path = str(path)
        # Serializes access to the single shared connection (dashboard + Telegram threads)
        self._lock = threading.RLock()

        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. This Store version requires PostgreSQL."
            )

        # autocommit avoids long-lived open transactions after plain SELECTs.
        # Every write uses an explicit transaction() block instead.
        self._conn = psycopg.connect(database_url, autocommit=True)

        try:
            with self._conn.transaction():
                # Advisory lock: serializes schema creation when several
                # instances start at the same time.
                self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (724193108,))
                for statement in _SCHEMA:
                    self._conn.execute(statement)
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- internal helpers ---

    def _one(self, query, args=()):
        """Run a SELECT and return the first row (or None)."""
        with self._lock:
            with self._conn.cursor() as cursor:
                cursor.execute(query, args or None)
                return cursor.fetchone()

    def _all(self, query, args=()):
        """Run a SELECT and return all rows."""
        with self._lock:
            with self._conn.cursor() as cursor:
                cursor.execute(query, args or None)
                return cursor.fetchall()

    def _write(self, query, args=()):
        """Run a single write statement inside its own transaction."""
        with self._lock:
            with self._conn.transaction():
                self._conn.execute(query, args or None)

    # --- processed messages ---

    def is_processed(self, source_chat, message_id: int) -> bool:
        """True if the message was forwarded successfully or deliberately skipped."""
        row = self._one(
            "SELECT status FROM processed WHERE source_chat=%s AND message_id=%s",
            (str(source_chat), int(message_id)),
        )
        return bool(row) and row[0] in (STATUS_OK, STATUS_SKIPPED)

    def mark(
        self,
        source_chat,
        message_ids: Iterable[int],
        status: str,
        target_ids: Optional[Iterable[int]] = None,
        info: str = "",
    ) -> None:
        """Store the result for a batch of messages and release their claims."""
        targets = ",".join(str(t) for t in (target_ids or []))
        rows = [
            (str(source_chat), int(mid), status, targets, info[:500], _now())
            for mid in message_ids
        ]
        if not rows:
            return

        # One transaction: result and claim release succeed or fail together.
        with self._lock:
            with self._conn.transaction():
                with self._conn.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO processed (
                            source_chat, message_id, status,
                            target_ids, info, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (source_chat, message_id)
                        DO UPDATE SET
                            status=excluded.status,
                            target_ids=excluded.target_ids,
                            info=excluded.info,
                            updated_at=excluded.updated_at
                        """,
                        rows,
                    )
                    # The result is stored now, so the reservation is no longer needed.
                    cursor.execute(
                        "DELETE FROM claims WHERE source_chat=%s AND message_id = ANY(%s)",
                        (str(source_chat), [r[1] for r in rows]),
                    )

    def failed_ids(self, source_chat) -> List[int]:
        """IDs of messages whose last transfer attempt failed (for retries)."""
        rows = self._all(
            """
            SELECT message_id FROM processed
            WHERE source_chat=%s AND status=%s
            ORDER BY message_id
            """,
            (str(source_chat), STATUS_FAILED),
        )
        return [row[0] for row in rows]

    def counts(self, source_chat=None) -> dict:
        """Number of messages per status, optionally for one source chat."""
        query = "SELECT status, COUNT(*) FROM processed"
        args = ()
        if source_chat is not None:
            query += " WHERE source_chat=%s"
            args = (str(source_chat),)

        rows = self._all(query + " GROUP BY status", args)
        result = {STATUS_OK: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
        result.update(dict(rows))
        return result

    def recent(self, limit: int = 20) -> List[dict]:
        """Most recently updated history entries (used by the dashboard)."""
        rows = self._all(
            """
            SELECT source_chat, message_id, status,
                   target_ids, info, updated_at
            FROM processed
            ORDER BY updated_at DESC, message_id DESC
            LIMIT %s
            """,
            (int(limit),),
        )
        keys = ("source_chat", "message_id", "status",
                "target_ids", "info", "updated_at")
        return [dict(zip(keys, row)) for row in rows]

    # --- claims (reservation against parallel transfers) ---

    def claim(self, source_chat, message_ids: Iterable[int], owner: str) -> bool:
        """Atomically reserve all given IDs for ``owner``.

        Returns False if another instance holds a valid claim on any of the IDs,
        or if any ID was already processed (ok/skipped) in the meantime.
        It is all-or-nothing: either every ID is reserved or none.
        """
        ids = [int(m) for m in message_ids]
        if not ids:
            return True
        chat = str(source_chat)

        try:
            with self._lock:
                with self._conn.transaction():
                    # Another instance may have finished this message after our scan.
                    done = self._conn.execute(
                        """
                        SELECT 1 FROM processed
                        WHERE source_chat=%s AND message_id = ANY(%s)
                          AND status IN (%s, %s)
                        LIMIT 1
                        """,
                        (chat, ids, STATUS_OK, STATUS_SKIPPED),
                    ).fetchone()
                    if done:
                        raise _ClaimDenied

                    for mid in ids:
                        # Insert a new claim, or take over a claim that is
                        # expired or already ours. RETURNING yields no row
                        # when a valid foreign claim blocks the update.
                        row = self._conn.execute(
                            """
                            INSERT INTO claims (source_chat, message_id, owner, expires_at)
                            VALUES (%s, %s, %s, now() + make_interval(secs => %s))
                            ON CONFLICT (source_chat, message_id) DO UPDATE
                                SET owner=excluded.owner,
                                    expires_at=excluded.expires_at
                                WHERE claims.expires_at < now()
                                   OR claims.owner = excluded.owner
                            RETURNING 1
                            """,
                            (chat, mid, owner, self.CLAIM_TTL_SECONDS),
                        ).fetchone()
                        if row is None:
                            # Leaving the transaction block via exception
                            # rolls back all claims made for this batch.
                            raise _ClaimDenied
        except _ClaimDenied:
            return False
        return True

    def release(self, source_chat, message_ids: Iterable[int]) -> None:
        """Give up reservations without storing a result (e.g. on abort)."""
        ids = [int(m) for m in message_ids]
        if ids:
            self._write(
                "DELETE FROM claims WHERE source_chat=%s AND message_id = ANY(%s)",
                (str(source_chat), ids),
            )

    # --- checkpoints (highest message processed without gaps) ---
    # Kept separately per source AND topic selection: a run over topic 5 only
    # must not advance the checkpoint for "all topics", otherwise messages of
    # other topics would be skipped. Duplicates are prevented independently
    # by the ``processed`` table (key: source + message ID).

    @staticmethod
    def checkpoint_key(source_chat, topic_id: Optional[int] = None) -> str:
        key = f"checkpoint:{source_chat}"
        return f"{key}:topic:{int(topic_id)}" if topic_id else key

    def get_checkpoint(self, source_chat, topic_id: Optional[int] = None) -> int:
        value = self.get_value(self.checkpoint_key(source_chat, topic_id))
        return int(value) if value else 0

    def set_checkpoint(self, source_chat, message_id: int,
                       topic_id: Optional[int] = None) -> None:
        message_id = int(message_id)
        if message_id <= 0:
            return

        # Atomic "only move forward": GREATEST is evaluated inside the database,
        # so another instance can never move the checkpoint backwards.
        self._write(
            """
            INSERT INTO kv (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET
                value = GREATEST(
                    COALESCE(NULLIF(kv.value, ''), '0')::bigint,
                    excluded.value::bigint
                )::text
            """,
            (self.checkpoint_key(source_chat, topic_id), str(message_id)),
        )

    def get_value(self, key: str) -> Optional[str]:
        row = self._one("SELECT value FROM kv WHERE key=%s", (key,))
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        self._write(
            """
            INSERT INTO kv (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    # --- topic mapping ---

    def get_topic(self, source_chat, source_topic: int, target_chat) -> Optional[int]:
        row = self._one(
            """
            SELECT target_topic FROM topics
            WHERE source_chat=%s AND source_topic=%s AND target_chat=%s
            """,
            (str(source_chat), int(source_topic), str(target_chat)),
        )
        return row[0] if row else None

    def get_topic_mapping(self, source_chat, source_topic: int, target_chat):
        """Return (target_topic, stored title) or None."""
        row = self._one(
            """
            SELECT target_topic, title FROM topics
            WHERE source_chat=%s AND source_topic=%s AND target_chat=%s
            """,
            (str(source_chat), int(source_topic), str(target_chat)),
        )
        return (row[0], row[1] or "") if row else None

    def set_topic(self, source_chat, source_topic: int, target_chat,
                  target_topic: int, title: str = "") -> None:
        self._write(
            """
            INSERT INTO topics (
                source_chat, source_topic, target_chat, target_topic, title
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_chat, source_topic, target_chat)
            DO UPDATE SET
                target_topic=excluded.target_topic,
                title=excluded.title
            """,
            (str(source_chat), int(source_topic), str(target_chat),
             int(target_topic), title),
        )

    def topics(self) -> List[dict]:
        rows = self._all(
            """
            SELECT source_chat, source_topic, target_chat, target_topic, title
            FROM topics
            """
        )
        keys = ("source_chat", "source_topic", "target_chat",
                "target_topic", "title")
        return [dict(zip(keys, row)) for row in rows]

    # --- reset ---

    def reset(self, source_chat=None, include_topics: bool = False) -> None:
        """Delete history and checkpoints. NOTE: affects ALL instances,
        because the database is shared."""
        with self._lock:
            with self._conn.transaction():
                if source_chat is None:
                    self._conn.execute("DELETE FROM processed")
                    self._conn.execute("DELETE FROM claims")
                    self._conn.execute(
                        "DELETE FROM kv WHERE key LIKE %s", ("checkpoint:%",)
                    )
                    if include_topics:
                        self._conn.execute("DELETE FROM topics")
                else:
                    self._conn.execute(
                        "DELETE FROM processed WHERE source_chat=%s",
                        (str(source_chat),),
                    )
                    self._conn.execute(
                        "DELETE FROM claims WHERE source_chat=%s",
                        (str(source_chat),),
                    )
                    # Delete the plain checkpoint and all per-topic checkpoints
                    key = self.checkpoint_key(source_chat)
                    self._conn.execute(
                        "DELETE FROM kv WHERE key=%s OR key LIKE %s",
                        (key, key + ":topic:%"),
                    )
                    if include_topics:
                        self._conn.execute(
                            "DELETE FROM topics WHERE source_chat=%s",
                            (str(source_chat),),
                        )

    # --- one-time SQLite import ---

    def import_sqlite(self, sqlite_path) -> dict:
        """Import an old SQLite history into EMPTY PostgreSQL tables.

        Never merges or overwrites existing data. Stop every forwarder
        instance before running this. The SQLite file is opened read-only
        and all PostgreSQL inserts happen in a single transaction, so a
        failure leaves the target database untouched.
        """
        source_path = Path(sqlite_path).resolve(strict=True)
        if not source_path.is_file():
            raise ValueError("The SQLite source must be a file.")

        # Fixed SQL (no user input) using the columns of the original schema.
        # Each entry: (table name, SELECT on SQLite, INSERT on PostgreSQL, COUNT on PostgreSQL)
        transfers = (
            (
                "processed",
                """
                SELECT source_chat, message_id, status,
                       target_ids, info, updated_at FROM processed
                """,
                """
                INSERT INTO processed (
                    source_chat, message_id, status,
                    target_ids, info, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                "SELECT COUNT(*) FROM processed",
            ),
            (
                "topics",
                """
                SELECT source_chat, source_topic, target_chat,
                       target_topic, title FROM topics
                """,
                """
                INSERT INTO topics (
                    source_chat, source_topic, target_chat,
                    target_topic, title
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                "SELECT COUNT(*) FROM topics",
            ),
            (
                "kv",
                "SELECT key, value FROM kv",
                "INSERT INTO kv (key, value) VALUES (%s, %s)",
                "SELECT COUNT(*) FROM kv",
            ),
        )

        # mode=ro: the original SQLite file can never be modified by the import
        source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
        try:
            source.execute("BEGIN")  # read all tables from one consistent snapshot
            imported = {}

            with self._lock:
                with self._conn.transaction():
                    # Exclusive lock: nobody can write while we verify and import
                    self._conn.execute(
                        "LOCK TABLE processed, topics, kv IN ACCESS EXCLUSIVE MODE"
                    )

                    # Safety check: refuse to import into non-empty tables
                    for name, _, _, count_query in transfers:
                        if self._conn.execute(count_query).fetchone()[0]:
                            raise RuntimeError(
                                f"Import aborted: PostgreSQL table '{name}' is not empty."
                            )

                    with self._conn.cursor() as destination:
                        for name, select_query, insert_query, count_query in transfers:
                            source_cursor = source.execute(select_query)
                            count = 0
                            try:
                                # Copy in chunks to keep memory usage low
                                while True:
                                    rows = source_cursor.fetchmany(1000)
                                    if not rows:
                                        break
                                    destination.executemany(insert_query, rows)
                                    count += len(rows)
                            finally:
                                source_cursor.close()

                            # Verify: imported row count must match PostgreSQL's count.
                            # A mismatch raises and rolls back the whole import.
                            actual = destination.execute(count_query).fetchone()[0]
                            if actual != count:
                                raise RuntimeError(
                                    f"Row count check failed for table '{name}'."
                                )
                            imported[name] = count

            return imported
        finally:
            source.close()


def main():
    """CLI entry point: python -m forwarder.store <path-to-old-forwarder.db>"""
    parser = argparse.ArgumentParser(
        description="Import an old SQLite history once into an empty PostgreSQL database."
    )
    parser.add_argument("sqlite_path", help="Path to the previous SQLite file")
    args = parser.parse_args()

    store = Store(args.sqlite_path)
    try:
        counts = store.import_sqlite(args.sqlite_path)
        print("Import finished; row counts verified:")
        for table, count in counts.items():
            print(f"  {table}: {count}")
    finally:
        store.close()


if __name__ == "__main__":
    main()