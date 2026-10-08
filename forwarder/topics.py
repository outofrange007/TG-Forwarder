"""Determines the target topic for a message.

TOPIC_MODE:
  none   - no topic (or "General" in a forum)
  fixed  - everything into TARGET_TOPIC_ID
  mirror - source topics are created in the target with the same name (and reused);
           the mapping is stored in the database (like topics.json in the original).
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional, Set

from . import tg_compat
from .media import GENERAL_TOPIC_ID, source_topic_id

log = logging.getLogger("forwarder.topics")

GENERAL_TITLE = "General"
_PLACEHOLDER_RE = re.compile(r"^Topic \d+$")


def placeholder_title(topic_id: int) -> str:
    return f"Topic {topic_id}"


def is_placeholder(title: Optional[str]) -> bool:
    return not (title or "").strip() or bool(_PLACEHOLDER_RE.match(title.strip()))


class TopicResolver:
    def __init__(self, client, settings, store, source_entity=None, target_entity=None,
                 source_key=None, target_key=None):
        self.client = client
        self.source_key = str(source_key if source_key is not None else settings.source_chat)
        self.target_key = str(target_key if target_key is not None else settings.target_chat)
        self.settings = settings
        self.store = store
        self.source_entity = source_entity
        self.target_entity = target_entity
        self._source_titles: Dict[int, str] = {}       # confirmed real titles
        self._listing: Optional[Dict[int, str]] = None  # full listing (fetched at most once)
        self._target_by_title: Optional[Dict[str, int]] = None
        self._checked: Set[int] = set()                 # mappings already checked for placeholder names

    @property
    def source_is_forum(self) -> bool:
        return bool(getattr(self.source_entity, "forum", False))

    async def resolve(self, message) -> Optional[int]:
        mode = self.settings.topic_mode
        if mode == "fixed":
            return self.settings.target_topic_id
        if mode != "mirror":
            return None
        src_topic = source_topic_id(message, self.source_is_forum)
        if src_topic is None or src_topic == GENERAL_TOPIC_ID:
            return None  # General -> General
        mapping = self.store.get_topic_mapping(self.source_key, src_topic, self.target_key)
        if mapping:
            target_topic, stored_title = mapping
            if src_topic not in self._checked:
                self._checked.add(src_topic)
                if is_placeholder(stored_title):
                    await self._repair_placeholder(src_topic, target_topic)
            return target_topic
        title = await self._source_title(src_topic)
        target_topic = await self._find_or_create(title, src_topic)
        if not target_topic:  # DRY_RUN: store nothing
            return None
        self._checked.add(src_topic)
        self.store.set_topic(self.source_key, src_topic, self.target_key, target_topic, title)
        log.info("Topic mapping: '%s' (%s) -> %s", title, src_topic, target_topic)
        return target_topic

    # ------------------------------------------------------------ source titles
    async def lookup_title(self, topic_id: int) -> Optional[str]:
        """Real title of a source topic or ``None``.

        Order: cache -> "General" for ID 1 -> GetForumTopicsByID -> full listing
        (with pagination). Failures are logged as WARNING, never swallowed silently.
        """
        if topic_id in self._source_titles:
            return self._source_titles[topic_id]
        if topic_id == GENERAL_TOPIC_ID:
            return GENERAL_TITLE
        reasons = []
        try:
            found = await tg_compat.get_forum_topics_by_id(self.client, self.source_entity, [topic_id])
            self._source_titles.update(found)
            if topic_id in found:
                return found[topic_id]
            reasons.append("GetForumTopicsByID: topic not in response")
        except Exception as exc:  # noqa: BLE001 - fall back to the full listing
            log.warning("Title lookup by ID for source topic %s failed: %s: %s",
                        topic_id, type(exc).__name__, exc)
            reasons.append(f"GetForumTopicsByID: {type(exc).__name__}: {exc}")
        if self._listing is None:
            try:
                self._listing = await tg_compat.list_forum_topics(self.client, self.source_entity)
                self._source_titles.update(self._listing)
            except Exception as exc:  # noqa: BLE001
                log.warning("Listing the source topics failed: %s: %s", type(exc).__name__, exc)
                reasons.append(f"GetForumTopics: {type(exc).__name__}: {exc}")
        if topic_id in self._source_titles:
            return self._source_titles[topic_id]
        if self._listing is not None:
            reasons.append(f"GetForumTopics: topic not among {len(self._listing)} listed topics")
        if not self.source_is_forum:
            reasons.append("source entity is not marked as forum")
        log.warning("Real title of source topic %s unknown (%s)", topic_id, "; ".join(reasons))
        return None

    async def _source_title(self, topic_id: int) -> str:
        title = await self.lookup_title(topic_id)
        if title:
            return title
        log.warning("Using placeholder name '%s' for source topic %s",
                    placeholder_title(topic_id), topic_id)
        return placeholder_title(topic_id)

    # ------------------------------------------------------------ target topics
    async def _target_titles(self) -> Dict[str, int]:
        if self._target_by_title is None:
            topics = await tg_compat.list_forum_topics(self.client, self.target_entity)
            self._target_by_title = {t.strip().lower(): tid for tid, t in topics.items()}
        return self._target_by_title

    async def _rename(self, target_topic: int, old: str, title: str) -> bool:
        if self.settings.dry_run:
            log.info("[DRY_RUN] Would rename target topic %s '%s' -> '%s'", target_topic, old, title)
            return False
        try:
            await tg_compat.edit_forum_topic(self.client, self.target_entity, target_topic, title)
        except Exception as exc:  # noqa: BLE001 - keep using the topic under its old name
            log.warning("Could not rename target topic %s '%s' -> '%s' (admin right "
                        "'Manage topics' needed?): %s: %s", target_topic, old, title,
                        type(exc).__name__, exc)
            return False
        log.info("Renamed target topic %s: '%s' -> '%s'", target_topic, old, title)
        if self._target_by_title is not None:
            self._target_by_title.pop(old.strip().lower(), None)
            self._target_by_title[title.strip().lower()] = target_topic
        return True

    async def _repair_placeholder(self, src_topic: int, target_topic: int) -> None:
        """Mapping was stored with a placeholder name: rename the target topic once the real title is known."""
        title = await self.lookup_title(src_topic)
        if not title:
            return
        old = placeholder_title(src_topic)
        if await self._rename(target_topic, old, title):
            self.store.set_topic(self.source_key, src_topic, self.target_key, target_topic, title)

    async def _find_or_create(self, title: str, src_topic: Optional[int] = None) -> int:
        existing = await self._target_titles()
        key = title.strip().lower()
        if key in existing:
            return existing[key]
        # an earlier run created "Topic <id>" (mapping lost, e.g. after a reset): rename instead of duplicating
        if src_topic is not None and not is_placeholder(title):
            old = placeholder_title(src_topic)
            old_id = existing.get(old.lower())
            if old_id:
                await self._rename(old_id, old, title)
                return old_id
        if self.settings.dry_run:
            log.info("[DRY_RUN] Would create topic '%s'", title)
            return 0
        topic_id = await tg_compat.create_forum_topic(self.client, self.target_entity, title)
        existing[key] = topic_id
        return topic_id
