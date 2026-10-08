"""Core: transfer photos and videos from a source chat to a target chat.

Flow (analogous to the upload loop of the original):
  1. Scan phase: collect candidates (filters, skip already processed ones)
  2. Transfer loop: send single messages and albums, report progress,
     record the result in the database
  3. Optional live mode: transfer new messages in real time

Sending strategy:
  MODE=forward - real forwarding (with/without "Forwarded from", HIDE_SENDER)
  MODE=copy    - re-send media by reference (no download, no forwarding note)
  Content protection - if forwarding is forbidden in the source chat, the media is
                  downloaded and re-uploaded (DOWNLOAD_FALLBACK=true), with progress & speed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional

from telethon import errors, events, utils
from telethon.tl import types

from . import tg_compat
from .media import (
    GENERAL_TOPIC_ID,
    Batch, build_caption, check_message, classify, file_name, file_size,
    group_batches, is_protected, message_link, source_topic_id,
)
from .state import MB, ProgressState
from .store import STATUS_FAILED, STATUS_OK, Store
from .topics import TopicResolver
from .upload_meta import (
    FileTooLargeError, check_upload_size, find_invalid_values, minimal_attributes, sanitize_attributes,
)

log = logging.getLogger("forwarder.engine")

_KEEP_ATTRS = (types.DocumentAttributeVideo, types.DocumentAttributeAnimated,
               types.DocumentAttributeFilename, types.DocumentAttributeImageSize)
_PROTECTED_ERRORS = tuple(
    e for e in (getattr(errors, "ChatForwardsRestrictedError", None),
                getattr(errors, "MediaInvalidError", None),
                getattr(errors, "FileReferenceExpiredError", None)) if e
)


class RetryExhausted(RuntimeError):
    pass


class Aborted(RuntimeError):
    pass


class ProtectedContentError(RuntimeError):
    """Content protection active and download fallback disabled (retrying makes no sense)."""


@dataclass
class _Upload:
    """Already uploaded file; builds the InputMedia from it (normal or emergency without metadata)."""
    file: object
    kind: str
    name: str
    mime: str = "application/octet-stream"
    attributes: list = field(default_factory=list)
    thumb: object = None

    def media(self, minimal: bool = False):
        if self.kind == "photo":
            return types.InputMediaUploadedPhoto(file=self.file)
        attrs = self.attributes
        if minimal:
            attrs = minimal_attributes(self.name)
            if self.kind in ("video", "animation", "video_note"):
                attrs.append(types.DocumentAttributeVideo(
                    duration=0, w=1, h=1, round_message=self.kind == "video_note" or None,
                    supports_streaming=self.kind == "video" or None))
            if self.kind == "animation":
                attrs.append(types.DocumentAttributeAnimated())
        return types.InputMediaUploadedDocument(
            file=self.file, mime_type=self.mime, attributes=attrs,
            thumb=None if minimal else self.thumb,
            force_file=self.kind in ("image_file", "video_file") or None,
            nosound_video=self.kind == "video" or None)


class Forwarder:
    def __init__(self, client, settings, store: Store, state: Optional[ProgressState] = None,
                 stop_event: Optional[threading.Event] = None):
        self.client = client
        self.settings = settings
        self.store = store
        self.state = state or ProgressState()
        self.stop_event = stop_event or threading.Event()
        self.source = None
        self.target = None
        self.source_key = str(settings.source_chat)
        self.target_key = str(settings.target_chat)
        self.source_title = ""
        self.scan_max_id = 0
        self.source_topic_title = ""
        self.topics: Optional[TopicResolver] = None
        self._premium: Optional[bool] = None

    # ------------------------------------------------------------------ setup
    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    async def prepare(self) -> None:
        self.state.update(status="Resolving chats ...")
        self.source = await self.client.get_entity(self.settings.source_chat)
        self.target = await self.client.get_entity(self.settings.target_chat)
        self.source_key = str(utils.get_peer_id(self.source))
        self.target_key = str(utils.get_peer_id(self.target))
        if self.source_key == self.target_key:
            raise ValueError("Source and target are identical")
        self.source_title = utils.get_display_name(self.source) or self.source_key
        topic = self.settings.source_topic_id
        if topic is not None:
            if not getattr(self.source, "forum", False):
                raise ValueError("SOURCE_TOPIC_ID is set, but the source is not a forum with topics")
            titles = {}
            if topic != GENERAL_TOPIC_ID:
                try:
                    titles = await tg_compat.get_forum_topics_by_id(self.client, self.source, [topic])
                except Exception as exc:  # noqa: BLE001 - fall back to the full listing
                    log.warning("Topic lookup by ID failed (%s: %s) - using the full listing",
                                type(exc).__name__, exc)
            if topic not in titles:
                titles = await tg_compat.list_forum_topics(self.client, self.source)
                if titles and topic not in titles and topic != GENERAL_TOPIC_ID:
                    raise ValueError(f"Topic {topic} does not exist in the source "
                                     f"(available: {', '.join(map(str, sorted(titles)))})")
            self.source_topic_title = titles.get(topic) or (
                "General" if topic == GENERAL_TOPIC_ID else f"Topic {topic}")
        self.topics = TopicResolver(self.client, self.settings, self.store, self.source,
                                    self.target, self.source_key, self.target_key)
        log.info("Source: %s (%s) | Source topic: %s | Target: %s (%s) | Mode: %s | Topics: %s",
                 self.source_title, self.source_key,
                 f"{self.source_topic_title} ({topic})" if topic else "all",
                 utils.get_display_name(self.target), self.target_key,
                 self.settings.mode, self.settings.topic_mode)

    # ------------------------------------------------------------ topic filter
    @property
    def topic_filter(self) -> Optional[int]:
        return self.settings.source_topic_id

    def in_selected_topic(self, msg) -> bool:
        """True if no topic selection is active or ``msg`` is in the selected source topic.

        What counts is ``reply_to.forum_topic`` with the top ID (``reply_to_top_id`` or
        ``reply_to_msg_id``), see ``media.source_topic_id``. Albums are therefore captured
        completely, because every album item carries the same topic marker.
        """
        if self.topic_filter is None:
            return True
        return source_topic_id(msg, getattr(self.source, "forum", False)) == self.topic_filter

    # ---------------------------------------------------------------- history
    async def collect_candidates(self) -> List:
        s = self.settings
        topic = self.topic_filter
        min_id = max(s.start_from_id - 1, 0)
        if s.resume:
            min_id = max(min_id, self.store.get_checkpoint(self.source_key, topic))
        max_id = (s.end_at_id + 1) if s.end_at_id else 0

        candidates = {}
        # retry previously failed messages
        failed = [i for i in self.store.failed_ids(self.source_key) if i <= min_id]
        if failed:
            for msg in await self.client.get_messages(self.source, ids=failed):
                if msg is not None and self.in_selected_topic(msg) and check_message(msg, s).accepted:
                    candidates[msg.id] = msg

        scanned = 0
        self.scan_max_id = min_id
        # Restrict to the topic thread on the server side (GetReplies). "General" (ID 1)
        # is not a real thread -> read the full history and filter client-side.
        reply_to = topic if topic and topic != GENERAL_TOPIC_ID else None
        async for msg in self.client.iter_messages(self.source, reverse=True, min_id=min_id,
                                                   max_id=max_id, reply_to=reply_to):
            if self.stopped:
                break
            scanned += 1
            self.scan_max_id = max(self.scan_max_id, msg.id)
            if scanned % 200 == 0:
                self.state.update(status=f"Scanning ... {scanned} messages, {len(candidates)} media")
            if not self.in_selected_topic(msg) or self.store.is_processed(self.source_key, msg.id):
                continue
            if check_message(msg, s).accepted:
                candidates[msg.id] = msg
        log.info("Scan: %d messages checked, %d media to transfer", scanned, len(candidates))
        return [candidates[k] for k in sorted(candidates)]

    async def run_history(self) -> dict:
        self.state.reset_job("history")
        self.state.update(status="Scanning source chat ...")
        candidates = await self.collect_candidates()
        batches = group_batches(candidates)
        self.state.update(total=len(candidates))
        if not candidates:
            self.state.update(status="Done - no new media")
        log.info("Transfer starting: %d media in %d shipments", len(candidates), len(batches))

        for index, batch in enumerate(batches):
            if self.stopped:
                break
            await self.process_batch(batch)
            if not self.settings.dry_run:
                self.store.set_checkpoint(self.source_key, batch.max_id, self.topic_filter)
            if index < len(batches) - 1:
                await self._sleep(self.settings.delay_seconds)

        if not self.stopped and not self.settings.dry_run:
            self.store.set_checkpoint(self.source_key, self.scan_max_id, self.topic_filter)
        summary = self.state.snapshot()
        log.info("Run finished | total=%d ok=%d skipped=%d failed=%d | %s",
                 summary["total"], summary["ok"], summary["skipped"], summary["failed"],
                 "aborted" if self.stopped else "complete")
        return summary

    # -------------------------------------------------------------------- live
    def _live_accepts(self, msg) -> bool:
        if not self.in_selected_topic(msg):
            return False
        if self.store.is_processed(self.source_key, msg.id):
            return False
        return check_message(msg, self.settings).accepted

    async def run_live(self) -> None:
        """Listens for new messages until ``stop_event`` is set."""
        queue: asyncio.Queue = asyncio.Queue()

        async def on_message(event):
            if getattr(event.message, "grouped_id", None):
                return  # albums arrive together via events.Album
            if self._live_accepts(event.message):
                await queue.put(Batch([event.message]))

        async def on_album(event):
            msgs = [m for m in event.messages if self._live_accepts(m)]
            for batch in group_batches(msgs):
                await queue.put(batch)

        handlers = [(on_message, events.NewMessage(chats=self.source)),
                    (on_album, events.Album(chats=self.source))]
        for cb, ev in handlers:
            self.client.add_event_handler(cb, ev)
        self.state.update(job="live", running=True, status="Live: waiting for new media ...")
        self.state.log("Live mode active")
        log.info("Live mode active - waiting for new messages in %s", self.source_title)
        try:
            while not self.stopped:
                try:
                    batch = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self.state.update(total=self.state.total + len(batch.messages))
                await self.process_batch(batch)
                self.state.update(status="Live: waiting for new media ...")
                await self._sleep(self.settings.delay_seconds)
        finally:
            for cb, ev in handlers:
                self.client.remove_event_handler(cb, ev)

    async def run(self, live: bool = False) -> dict:
        await self.prepare()
        summary = await self.run_history()
        if live and not self.stopped:
            await self.run_live()
            summary = self.state.snapshot()
        return summary

    # ------------------------------------------------------------ processing
    def _label(self, batch: Batch) -> str:
        label = file_name(batch.messages[0])
        return f"{label} (+{len(batch.messages) - 1} in album)" if batch.is_album else label

    async def process_batch(self, batch: Batch) -> str:
        n = len(batch.messages)
        label = self._label(batch)
        self.state.update(current_item=label, status="Transferring ...", phase="sending",
                          file_progress=0.0, file_size_mb=batch.total_size / MB)
        try:
            topic = await self.topics.resolve(batch.messages[0]) if self.topics else None
            if topic is not None and not 0 < int(topic) <= 2**31 - 1:
                # reply_to is packed as a 32-bit value -> otherwise "'i' format requires ..."
                msg = (f"Target topic ID {topic} is invalid (allowed: 1 to 2147483647). "
                       "TARGET_TOPIC_ID probably contains a chat ID - please enter the topic ID "
                       "from the topic link (e.g. 55 in t.me/c/1234567890/55).")
                self.state.log(msg, "error")
                raise Aborted(msg)
            if self.settings.dry_run:
                log.info("[DRY_RUN] %s -> topic %s (IDs %s)", label, topic, batch.ids)
                self.state.item_finished("ok", n)
                self.state.log(f"[Test run] {label}")
                return "dry_run"
            self._last_sent_text = None
            target_ids = await self._with_retry(lambda: self._send(batch, topic), label)
        except Aborted:
            raise
        except Exception as exc:  # a single broken message must not end the run
            reason = describe_error(exc)
            if isinstance(exc, FileTooLargeError):
                log.warning("Skipped: %s", reason)
            else:
                log.exception("Failed: %s (IDs %s)", label, batch.ids)
            self.store.mark(self.source_key, batch.ids, STATUS_FAILED, info=f"{type(exc).__name__}: {reason}")
            self.state.item_finished("failed", n)
            self.state.log(f"Error at {label}: {reason}", "error")
            if not self._connected():
                raise Aborted("Connection lost - run aborted") from exc
            return STATUS_FAILED
        self.store.mark(self.source_key, batch.ids, STATUS_OK, target_ids=target_ids)
        self.state.item_finished("ok", n)
        src_len = sum(len(getattr(m, "message", "") or "") for m in batch.messages)
        sent_len = getattr(self, "_last_sent_text", None)
        text_info = f"text source {src_len} / target {sent_len if sent_len is not None else '?'} chars"
        self.state.log(f"Transferred: {label} ({text_info})")
        log.info("Transferred: %s (IDs %s -> %s) | %s", label, batch.ids, target_ids, text_info)
        if src_len and sent_len == 0:
            log.warning("Text lost: %s (IDs %s) - source had %d chars, target 0",
                        label, batch.ids, src_len)
        return STATUS_OK

    async def _send_text(self, batch: Batch, topic: Optional[int]) -> List[int]:
        m = batch.messages[0]
        text = build_caption(m, self.settings.caption_template, self.source_title,
                             message_link(self.source, m.id), max_len=4096)
        if not text:
            text = getattr(m, "message", "") or ""
        plain = self.settings.caption_template.strip() == "{caption}" and text == (m.message or "").strip()
        kwargs = {"formatting_entities": m.entities} if plain and getattr(m, "entities", None) else {}
        result = await self.client.send_message(self.target, text, reply_to=topic or None, **kwargs)
        return self._ids(result)

    async def _send(self, batch: Batch, topic: Optional[int]) -> List[int]:
        if classify(batch.messages[0]) is None:      # text-only message
            return await self._send_text(batch, topic)
        protected = is_protected(batch.messages[0], self.source)
        if not protected:
            try:
                if self.settings.mode == "forward":
                    return await tg_compat.forward_messages(
                        self.client, self.source, self.target, batch.ids,
                        top_msg_id=topic, drop_author=self.settings.hide_sender)
                return await self._copy(batch, topic)
            except _PROTECTED_ERRORS as exc:
                log.info("Direct sending not possible (%s) - using download fallback", type(exc).__name__)
                protected = True
        if not self.settings.download_fallback:
            raise ProtectedContentError("Source chat has content protection and DOWNLOAD_FALLBACK=false")
        return await self._reupload(batch, topic)

    def _captions(self, batch: Batch):
        template = self.settings.caption_template
        captions, entities = [], []
        for m in batch.messages:
            link = message_link(self.source, m.id)
            captions.append(build_caption(m, template, self.source_title, link))
            # keep the original formatting (bold, links ...) only if the text is unchanged
            plain = template.strip() == "{caption}" and len(getattr(m, "message", "") or "") <= 1024
            entities.append(getattr(m, "entities", None) if plain else None)
        return captions, entities

    def _ids(self, result) -> List[int]:
        if result is None:
            return []
        items = result if isinstance(result, list) else [result]
        # remember for diagnostics which text actually arrived in the target
        self._last_sent_text = (sum(len(getattr(r, "message", "") or "") for r in items)
                                if any(hasattr(r, "message") for r in items) else None)
        return [getattr(r, "id", None) for r in items if getattr(r, "id", None) is not None]

    async def _copy(self, batch: Batch, topic: Optional[int]) -> List[int]:
        captions, entities = self._captions(batch)
        if batch.is_album:
            kwargs = {}
            if any(entities):
                kwargs["formatting_entities"] = [e or [] for e in entities]
            result = await self.client.send_file(
                self.target, file=[m.media for m in batch.messages], caption=captions,
                reply_to=topic or None, **kwargs)
        else:
            m = batch.messages[0]
            kwargs = {"formatting_entities": entities[0]} if entities[0] else {}
            result = await self.client.send_file(
                self.target, file=m.media, caption=captions[0], reply_to=topic or None,
                video_note=classify(m) == "video_note", **kwargs)
        return self._ids(result)

    async def _is_premium(self) -> bool:
        """Premium accounts may upload up to 4000 MB (queried once, error = not premium)."""
        if self._premium is None:
            try:
                me = await self.client.get_me()
                self._premium = bool(getattr(me, "premium", False))
            except Exception:  # noqa: BLE001 - only relevant for the size limit
                self._premium = False
        return self._premium

    def _upload_attributes(self, m, path: str, kind: str, thumb):
        """Attributes for re-uploading: keep the original, detect the rest, validate everything."""
        doc = getattr(m, "document", None)
        kept = [a for a in getattr(doc, "attributes", None) or [] if isinstance(a, _KEEP_ATTRS)]
        mime = getattr(doc, "mime_type", None)
        try:
            attrs, mime = utils.get_attributes(
                path, attributes=kept, mime_type=mime,
                force_document=kind in ("image_file", "video_file"),
                video_note=kind == "video_note", supports_streaming=kind in ("video", "video_file"),
                thumb=thumb)
        except Exception as exc:  # noqa: BLE001 - a broken file/hachoir must not prevent the upload
            log.warning("Metadata detection for %s failed (%s) - using original attributes",
                        path, exc)
            attrs, mime = list(kept), mime or "application/octet-stream"
        attrs = sanitize_attributes(attrs)
        if kind in ("video", "animation", "video_note") and \
                not any(isinstance(a, types.DocumentAttributeVideo) for a in attrs):
            attrs.append(types.DocumentAttributeVideo(duration=0, w=1, h=1,
                                                      round_message=kind == "video_note" or None))
        for a in attrs:
            if kind == "video" and isinstance(a, types.DocumentAttributeVideo):
                a.supports_streaming = True
        if not any(isinstance(a, types.DocumentAttributeFilename) for a in attrs):
            attrs.append(types.DocumentAttributeFilename(Path(path).name))
        return attrs, mime or "application/octet-stream"

    async def _upload_one(self, m, path: str, thumb, progress) -> "_Upload":
        kind = classify(m) or "doc"
        handle = await self.client.upload_file(path, progress_callback=progress)
        if kind == "photo":
            return _Upload(handle, kind, Path(path).name)
        thumb_handle = None
        if thumb:
            try:
                thumb_handle = await self.client.upload_file(thumb)
            except Exception as exc:  # noqa: BLE001 - continue without a thumbnail
                log.warning("Thumbnail could not be uploaded: %s", exc)
        attrs, mime = self._upload_attributes(m, path, kind, thumb)
        return _Upload(handle, kind, Path(path).name, mime, attrs, thumb_handle)

    async def _reupload(self, batch: Batch, topic: Optional[int]) -> List[int]:
        premium = await self._is_premium()
        for m in batch.messages:          # check before downloading (saves time and space)
            check_upload_size(file_size(m), premium, file_name(m))
        tmp = Path(self.settings.tmp_path) / f"{self.source_key}_{batch.ids[0]}"
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            paths, thumbs = [], []
            for i, m in enumerate(batch.messages, 1):
                self.state.update(status=f"Downloading ({i}/{len(batch.messages)}) ...")
                path = await self.client.download_media(
                    m, file=str(tmp) + "/", progress_callback=self.state.progress_callback("Download"))
                if not path:
                    raise RuntimeError(f"Download failed (message {m.id})")
                if os.path.exists(path):
                    check_upload_size(os.path.getsize(path), premium, file_name(m))
                paths.append(path)
                thumb = None
                doc = getattr(m, "document", None)
                if doc is not None and getattr(doc, "thumbs", None):
                    thumb = await self.client.download_media(m, file=str(tmp / f"thumb_{m.id}.jpg"), thumb=-1)
                thumbs.append(thumb)

            captions, entities = self._captions(batch)
            self.state.update(status="Uploading ...")
            n = len(batch.messages)
            if batch.is_album:
                cb = self.state.progress_callback("Upload", count_mode=True)
                uploads = []
                for i, (m, path, thumb) in enumerate(zip(batch.messages, paths, thumbs)):
                    uploads.append(await self._upload_one(
                        m, path, thumb, lambda cur, tot, i=i: cb(i + (cur / tot if tot else 1), n)))
            else:
                uploads = [await self._upload_one(batch.messages[0], paths[0], thumbs[0],
                                                  self.state.progress_callback("Upload"))]

            async def send(minimal: bool):
                media = [u.media(minimal) for u in uploads]
                if batch.is_album:
                    kwargs = {"formatting_entities": [e or [] for e in entities]} if any(entities) else {}
                    return await self.client.send_file(self.target, file=media, caption=captions,
                                                       reply_to=topic or None, **kwargs)
                kwargs = {"formatting_entities": entities[0]} if entities[0] else {}
                return await self.client.send_file(self.target, file=media[0], caption=captions[0],
                                                   reply_to=topic or None, **kwargs)

            try:
                result = await send(minimal=False)
            except struct.error as exc:
                # safety net: a value does not fit into a 32-bit field -> send without metadata
                bad = ", ".join(find_invalid_values([u.media(False) for u in uploads])) or str(exc)
                log.warning("Invalid metadata at %s (%s) - re-sending without attributes",
                            self._label(batch), bad)
                self.state.log(f"Invalid metadata ({bad}) - sending without attributes", "warn")
                result = await send(minimal=True)
            return self._ids(result)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------- retry/connection
    def _connected(self) -> bool:
        checker = getattr(self.client, "is_connected", None)
        return bool(checker()) if callable(checker) else True

    async def _sleep(self, seconds: float) -> None:
        """Interruptible wait (the stop button reacts immediately)."""
        remaining = float(seconds)
        while remaining > 0 and not self.stopped:
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    async def _with_retry(self, factory, label: str = ""):
        """Sends with retries: wait out FloodWait, reconnect after connection drops."""
        attempts = max(1, self.settings.max_retries)
        last_exc = None
        for attempt in range(1, attempts + 1):
            try:
                if not self._connected():
                    log.warning("Connection lost - reconnecting (attempt %d/%d)", attempt, attempts)
                    await self.client.connect()
                    if not await self.client.is_user_authorized():
                        raise Aborted("Session no longer authorized after reconnect")
                return await factory()
            except errors.FloodWaitError as exc:
                wait_s = int(getattr(exc, "seconds", 30) or 30)
                log.warning("FloodWait: pausing %ds (%s)", wait_s, label)
                self.state.update(status=f"FloodWait: waiting {wait_s}s ...")
                self.state.log(f"FloodWait {wait_s}s", "warn")
                await self._sleep(wait_s + 1)
                last_exc = exc
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                log.warning("Connection error at %s: %s (attempt %d/%d)", label, exc, attempt, attempts)
                await self._sleep(2 * attempt)
                last_exc = exc
            if self.stopped:
                break
        raise RetryExhausted(f"Gave up after {attempts} attempts: {last_exc}")


def describe_error(exc: Exception) -> str:
    """Understandable error message for log and dashboard."""
    if isinstance(exc, struct.error):
        return (f"A value does not fit into a 32-bit field of the Telegram protocol ({exc}). "
                "Most common cause: invalid target topic ID (e.g. chat ID instead of topic ID), "
                "less often broken video metadata; see the log file for details")
    return str(exc) or type(exc).__name__


async def list_source_topics(client, chat) -> dict:
    """Topics of a (source) group for dashboard/CLI: {forum, title, topics:[{id, title}]}."""
    entity = await client.get_entity(chat)
    forum = bool(getattr(entity, "forum", False))
    topics = await tg_compat.list_forum_topics(client, entity) if forum else {}
    return {
        "chat_id": utils.get_peer_id(entity),
        "title": utils.get_display_name(entity),
        "forum": forum,
        "topics": [{"id": tid, "title": title} for tid, title in sorted(topics.items())],
    }


async def list_dialogs(client, limit: int = 300) -> List[dict]:
    """Groups/channels of the account (for finding the chat IDs)."""
    result = []
    async for d in client.iter_dialogs(limit=limit):
        if not (d.is_group or d.is_channel):
            continue
        ent = d.entity
        result.append({
            "id": d.id,
            "title": d.name,
            "type": "Channel" if d.is_channel and not d.is_group else "Group",
            "forum": bool(getattr(ent, "forum", False)),
            "username": getattr(ent, "username", None),
            "protected": bool(getattr(ent, "noforwards", False)),
        })
    return result


__all__ = ["Forwarder", "list_dialogs", "list_source_topics", "RetryExhausted", "Aborted", "ProtectedContentError"]
