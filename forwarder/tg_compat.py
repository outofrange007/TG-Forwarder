"""Wraps Telegram API calls that vary between Telethon versions.

Forum topic functions lived under ``channels`` in older API layers and under
``messages`` in newer ones - similar to ``build_video_attribute`` in the original.
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

from telethon.tl import functions, types


def _request_cls(name: str):
    for ns in (functions.messages, functions.channels):
        cls = getattr(ns, name, None)
        if cls is not None:
            return cls
    raise RuntimeError(f"This Telethon version does not support {name} - please update Telethon")


def _peer_kw(cls) -> str:
    """messages.* expects ``peer``, channels.* expects ``channel``."""
    return "peer" if cls.__module__.endswith("messages") else "channel"


def _topic_kwargs(cls, peer) -> dict:
    return {_peer_kw(cls): peer}


def _titles(topics) -> Dict[int, str]:
    """{id: title} of real topics (``ForumTopicDeleted`` and empty titles are skipped)."""
    return {t.id: t.title for t in topics or []
            if isinstance(t, types.ForumTopic) and (t.title or "").strip()}


async def get_forum_topics_by_id(client, entity, topic_ids: List[int]) -> Dict[int, str]:
    """Exact lookup of specific topics (messages/channels.GetForumTopicsByID)."""
    if entity is None or not getattr(entity, "forum", False) or not topic_ids:
        return {}
    cls = _request_cls("GetForumTopicsByIDRequest")
    peer = await client.get_input_entity(entity)
    ids = [int(i) for i in topic_ids]
    result: Dict[int, str] = {}
    for i in range(0, len(ids), 100):
        resp = await client(cls(**_topic_kwargs(cls, peer), topics=ids[i:i + 100]))
        result.update(_titles(resp.topics))
    return result


async def list_forum_topics(client, entity, limit: int = 100, max_pages: int = 500) -> Dict[int, str]:
    """Returns {topic_id: title} of ALL topics of a forum (empty if not a forum).

    Pagination follows the API: the next page starts after the last topic, with
    ``offset_date``/``offset_id`` of that topic's *last message* (from
    ``resp.messages``) - not the topic creation date. The server may return
    fewer topics than ``limit`` per page, so the loop ends via ``resp.count`` or
    when a page brings nothing new.
    """
    if entity is None or not getattr(entity, "forum", False):
        return {}
    cls = _request_cls("GetForumTopicsRequest")
    peer = await client.get_input_entity(entity)
    result: Dict[int, str] = {}
    seen = set()
    offset_date, offset_id, offset_topic = None, 0, 0
    for _ in range(max_pages):
        resp = await client(cls(**_topic_kwargs(cls, peer), offset_date=offset_date,
                                offset_id=offset_id, offset_topic=offset_topic, limit=limit))
        page = [t for t in resp.topics or [] if getattr(t, "id", None) is not None]
        new = [t for t in page if t.id not in seen]
        if not new:
            break
        seen.update(t.id for t in new)
        result.update(_titles(new))
        total = getattr(resp, "count", None)
        if total is not None and len(seen) >= total:
            break
        dates = {m.id: m.date for m in getattr(resp, "messages", None) or []
                 if getattr(m, "id", None) is not None}
        last = page[-1]
        offset_topic = last.id
        offset_id = getattr(last, "top_message", 0) or 0
        offset_date = dates.get(offset_id) or getattr(last, "date", None)
    return result


async def edit_forum_topic(client, entity, topic_id: int, title: str) -> None:
    """Renames a topic (needs the admin right "Manage topics")."""
    cls = _request_cls("EditForumTopicRequest")
    peer = await client.get_input_entity(entity)
    await client(cls(**_topic_kwargs(cls, peer), topic_id=int(topic_id), title=title[:128]))


async def create_forum_topic(client, entity, title: str) -> int:
    cls = _request_cls("CreateForumTopicRequest")
    peer = await client.get_input_entity(entity)
    resp = await client(cls(**_topic_kwargs(cls, peer), title=title[:128],
                            random_id=random.randrange(-2**63, 2**63)))
    ids = new_message_ids(resp)
    if not ids:
        raise RuntimeError(f"Topic '{title}' could not be created")
    return ids[0]  # ID of the topic's start message = topic ID


def new_message_ids(updates) -> List[int]:
    """Extracts the IDs of newly created messages from an Updates object."""
    ids: List[int] = []
    for upd in getattr(updates, "updates", None) or []:
        if isinstance(upd, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
            ids.append(upd.message.id)
    return ids


async def forward_messages(client, source, target, message_ids: List[int],
                           top_msg_id: Optional[int] = None, drop_author: bool = False) -> List[int]:
    """Forwards messages - optionally into a forum topic (``top_msg_id``).

    ``TelegramClient.forward_messages`` knows no topics, hence the raw request.
    """
    req = functions.messages.ForwardMessagesRequest(
        from_peer=await client.get_input_entity(source),
        id=list(message_ids),
        to_peer=await client.get_input_entity(target),
        random_id=[random.randrange(-2**63, 2**63) for _ in message_ids],
        drop_author=drop_author or None,
        top_msg_id=top_msg_id or None,
    )
    return new_message_ids(await client(req))
