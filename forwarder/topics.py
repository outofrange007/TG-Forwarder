"""Determines the target topic for a message.

TOPIC_MODE:
  none   - no topic (or "General" in a forum)
  fixed  - everything into TARGET_TOPIC_ID
  mirror - source topics are created in the target with the same name (and reused);
           the mapping is stored in the database (like topics.json in the original).
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from . import tg_compat
from .media import GENERAL_TOPIC_ID, source_topic_id

log = logging.getLogger("forwarder.topics")


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
        self._source_titles: Optional[Dict[int, str]] = None
        self._target_by_title: Optional[Dict[str, int]] = None

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
        cached = self.store.get_topic(self.source_key, src_topic, self.target_key)
        if cached:
            return cached
        title = await self._source_title(src_topic)
        target_topic = await self._find_or_create(title)
        if not target_topic:  # DRY_RUN: store nothing
            return None
        self.store.set_topic(self.source_key, src_topic, self.target_key, target_topic, title)
        log.info("Topic mapping: '%s' (%s) -> %s", title, src_topic, target_topic)
        return target_topic

    async def _source_title(self, topic_id: int) -> str:
        if self._source_titles is None:
            self._source_titles = await tg_compat.list_forum_topics(self.client, self.source_entity)
        return self._source_titles.get(topic_id) or f"Topic {topic_id}"

    async def _find_or_create(self, title: str) -> int:
        if self._target_by_title is None:
            topics = await tg_compat.list_forum_topics(self.client, self.target_entity)
            self._target_by_title = {t.strip().lower(): tid for tid, t in topics.items()}
        key = title.strip().lower()
        if key in self._target_by_title:
            return self._target_by_title[key]
        if self.settings.dry_run:
            log.info("[DRY_RUN] Would create topic '%s'", title)
            return 0
        topic_id = await tg_compat.create_forum_topic(self.client, self.target_entity, title)
        self._target_by_title[key] = topic_id
        return topic_id
