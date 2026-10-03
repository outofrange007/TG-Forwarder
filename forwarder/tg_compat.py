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


async def list_forum_topics(client, entity, limit: int = 100) -> Dict[int, str]:
    """Returns {topic_id: title} of a forum (empty if not a forum)."""
    if entity is None or not getattr(entity, "forum", False):
        return {}
    cls = _request_cls("GetForumTopicsRequest")
    peer = await client.get_input_entity(entity)
    result: Dict[int, str] = {}
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        resp = await client(cls(**{_peer_kw(cls): peer}, offset_date=offset_date,
                                offset_id=offset_id, offset_topic=offset_topic, limit=limit))
        topics = [t for t in resp.topics if isinstance(t, types.ForumTopic)]
        for t in topics:
            result[t.id] = t.title
        if len(resp.topics) < limit or not topics:
            break
        last = topics[-1]
        offset_topic, offset_id, offset_date = last.id, last.top_message, last.date
    return result


async def create_forum_topic(client, entity, title: str) -> int:
    cls = _request_cls("CreateForumTopicRequest")
    peer = await client.get_input_entity(entity)
    resp = await client(cls(**{_peer_kw(cls): peer}, title=title[:128],
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
