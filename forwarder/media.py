"""Pure logic around messages: media type, filters, albums, captions.

All functions work with Telethon ``Message`` objects but only access a few
attributes (duck typing), so they can be tested without Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional

GENERAL_TOPIC_ID = 1


def classify(message) -> Optional[str]:
    """Returns the media type of a message or ``None`` (no photo/video)."""
    if message is None or getattr(message, "media", None) is None:
        return None
    # Telethon exposes the preview photo/video of a link (web page) via .photo/.document,
    # but it cannot be sent as a file. Link previews are not media of the message itself.
    if type(message.media).__name__ in ("MessageMediaWebPage", "WebPage"):
        return None
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "gif", None) is not None:
        return "animation"
    if getattr(message, "video_note", None) is not None:
        return "video_note"
    if getattr(message, "video", None) is not None:
        return "video"
    document = getattr(message, "document", None)
    if document is not None:
        mime = (getattr(document, "mime_type", "") or "").lower()
        if mime.startswith("image/") and mime != "image/webp":  # webp = usually a sticker
            return "image_file"
        if mime.startswith("video/"):
            return "video_file"
    return None


def is_text_only(message) -> bool:
    """Text-only message (no media, just text; a web preview does not count as media)."""
    if message is None or not (getattr(message, "message", "") or "").strip():
        return False
    media = getattr(message, "media", None)
    return media is None or type(media).__name__ == "MessageMediaWebPage"


def file_size(message) -> int:
    f = getattr(message, "file", None)
    size = getattr(f, "size", None) if f is not None else None
    return int(size or 0)


def file_name(message) -> str:
    f = getattr(message, "file", None)
    name = getattr(f, "name", None) if f is not None else None
    if classify(message) is None and is_text_only(message):
        return f"Text {getattr(message, 'id', '?')}"
    kind = classify(message) or "media"
    return name or f"{kind}_{getattr(message, 'id', '?')}"


def is_protected(message, chat=None) -> bool:
    """Is content protection active (forwarding/saving forbidden in the source chat)?"""
    return bool(getattr(message, "noforwards", False) or getattr(chat, "noforwards", False))


def source_topic_id(message, source_is_forum: bool = True) -> Optional[int]:
    """Determines the forum topic of a message.

    Messages in a topic carry ``reply_to.forum_topic``; ``reply_to_top_id`` is set
    if the message is additionally a reply, otherwise the topic ID is in
    ``reply_to_msg_id``. Without a marker it belongs to "General" (1).
    """
    if not source_is_forum:
        return None
    reply = getattr(message, "reply_to", None)
    if reply is not None and getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
    return GENERAL_TOPIC_ID


@dataclass
class FilterResult:
    accepted: bool
    reason: str = ""


def check_message(message, settings) -> FilterResult:
    """Checks a message against the filters from the settings."""
    kind = classify(message)
    if kind is None:
        if not (getattr(settings, "copy_text", False) and is_text_only(message)):
            return FilterResult(False, "no photo/video")
    elif kind not in settings.media_types:
        return FilterResult(False, f"media type {kind} not selected")
    mid = int(getattr(message, "id", 0))
    if settings.start_from_id and mid < settings.start_from_id:
        return FilterResult(False, "before START_FROM_ID")
    if settings.end_at_id and mid > settings.end_at_id:
        return FilterResult(False, "after END_AT_ID")
    date = getattr(message, "date", None)
    if date is not None:
        if settings.date_from and date < settings.date_from:
            return FilterResult(False, "before DATE_FROM")
        if settings.date_to and date > settings.date_to:
            return FilterResult(False, "after DATE_TO")
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if settings.max_file_size_mb and file_size(message) > max_bytes:
        return FilterResult(False, f"larger than {settings.max_file_size_mb:.0f} MB")
    return FilterResult(True)


@dataclass
class Batch:
    """One unit to send: a single message or a complete album (grouped_id)."""
    messages: List = field(default_factory=list)

    @property
    def ids(self) -> List[int]:
        return [m.id for m in self.messages]

    @property
    def max_id(self) -> int:
        return max(self.ids) if self.messages else 0

    @property
    def grouped_id(self):
        return getattr(self.messages[0], "grouped_id", None) if self.messages else None

    @property
    def is_album(self) -> bool:
        return len(self.messages) > 1

    @property
    def total_size(self) -> int:
        return sum(file_size(m) for m in self.messages)


def group_batches(messages: Iterable, max_album: int = 10) -> List[Batch]:
    """Combines consecutive messages with the same ``grouped_id`` into albums.

    Telegram allows at most 10 media items per album; larger groups are split.
    The order (ascending IDs) is preserved.
    """
    batches: List[Batch] = []
    for msg in sorted(messages, key=lambda m: m.id):
        gid = getattr(msg, "grouped_id", None)
        last = batches[-1] if batches else None
        if (gid is not None and last is not None and last.grouped_id == gid
                and len(last.messages) < max_album):
            last.messages.append(msg)
        else:
            batches.append(Batch([msg]))
    return batches


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def build_caption(message, template: str, source_title: str = "", source_link: str = "",
                  max_len: int = 1024) -> str:
    """Builds the caption from ``CAPTION_TEMPLATE``.

    Placeholders: {caption} {filename} {source} {link} {id} {date}. Unknown placeholders
    are left unchanged; an empty template removes the caption.
    """
    if not template:
        return ""
    date = getattr(message, "date", None)
    values = _SafeDict(
        caption=getattr(message, "message", "") or "",
        filename=getattr(getattr(message, "file", None), "name", None) or "",
        source=source_title or "",
        link=source_link or "",
        id=getattr(message, "id", ""),
        date=date.strftime("%Y-%m-%d %H:%M") if date else "",
    )
    try:
        text = template.format_map(values)
    except (ValueError, IndexError):  # e.g. a single curly brace
        text = template.replace("{caption}", values["caption"])
    text = text.strip()
    if len(text) > max_len:  # Telegram limit for captions
        text = text[: max_len - 3].rstrip() + "..."
    return text


def message_link(chat, message_id: int) -> str:
    username = getattr(chat, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    chat_id = getattr(chat, "id", None)
    return f"https://t.me/c/{chat_id}/{message_id}" if chat_id else ""
