"""Test doubles: simulated Telethon messages and a fake client without network."""

from datetime import datetime, timezone
from types import SimpleNamespace

from telethon.tl import types


def make_msg(mid, kind="photo", grouped_id=None, text="", size=1000, date=None,
             noforwards=False, topic=None, mime=None, name=None):
    photo = video = gif = video_note = document = None
    if kind == "photo":
        photo = object()
    elif kind in ("video", "animation", "video_note", "image_file", "video_file", "doc"):
        mime = mime or {"video": "video/mp4", "animation": "video/mp4", "video_note": "video/mp4",
                        "image_file": "image/jpeg", "video_file": "video/x-matroska",
                        "doc": "application/pdf"}[kind]
        document = SimpleNamespace(mime_type=mime, attributes=[], thumbs=None)
        if kind == "video":
            video = document
        elif kind == "animation":
            gif = document
        elif kind == "video_note":
            video_note = document
    media = None if kind in (None, "text") else SimpleNamespace(kind=kind, mid=mid)
    reply_to = SimpleNamespace(forum_topic=True, reply_to_msg_id=topic, reply_to_top_id=None) if topic else None
    return SimpleNamespace(
        id=mid, media=media, photo=photo, video=video, gif=gif, video_note=video_note,
        document=document, grouped_id=grouped_id, message=text, entities=None,
        date=date or datetime(2026, 1, 1, tzinfo=timezone.utc), noforwards=noforwards,
        reply_to=reply_to, file=SimpleNamespace(size=size, name=name),
    )


class FakeClient:
    """Mimics the subset of ``TelegramClient`` used by the engine."""

    def __init__(self, messages=(), source=None, target=None, fail_times=0, fail_exc=None,
                 forward_restricted=False, premium=False, struct_fail_once=False):
        self.messages = {m.id: m for m in messages}
        self.source = source or types.Channel(id=111, title="Source", photo=types.ChatPhotoEmpty(),
                                              date=None, access_hash=1, megagroup=True)
        self.target = target or types.Channel(id=222, title="Target", photo=types.ChatPhotoEmpty(),
                                              date=None, access_hash=2, megagroup=True)
        self.sent = []          # (art, kwargs)
        self.requests = []
        self.fail_times = fail_times
        self.fail_exc = fail_exc
        self.forward_restricted = forward_restricted
        self.connected = True
        self.next_id = 1000
        self.handlers = []
        self.premium = premium
        self.uploads = []        # (path, progress_callback set?)
        self.struct_fail_once = struct_fail_once

    # --- Entities ---
    async def get_entity(self, ref):
        return self.source if str(ref) in ("source", "-100111") else self.target

    async def get_input_entity(self, entity):
        return types.InputPeerChannel(entity.id, entity.access_hash)

    # --- Reading messages ---
    async def iter_messages(self, entity, reverse=False, min_id=0, max_id=0, reply_to=None, **kw):
        for mid in sorted(self.messages, reverse=not reverse):
            if mid <= min_id or (max_id and mid >= max_id):
                continue
            yield self.messages[mid]

    async def get_messages(self, entity, ids=None):
        return [self.messages.get(i) for i in ids]

    async def get_me(self):
        return SimpleNamespace(premium=self.premium)

    # --- Sending ---
    async def upload_file(self, file, progress_callback=None, **kw):
        self.uploads.append((file, progress_callback is not None))
        if progress_callback:
            progress_callback(100, 100)
        return types.InputFile(id=len(self.uploads), parts=1, name=str(file).rsplit("/", 1)[-1], md5_checksum="")

    @staticmethod
    def _uploaded(f):
        return isinstance(f, (str, types.InputMediaUploadedDocument, types.InputMediaUploadedPhoto))

    def _maybe_fail(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_exc

    def _new(self):
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def send_file(self, entity, file=None, **kwargs):
        self._maybe_fail()
        files = file if isinstance(file, list) else [file]
        if self.forward_restricted and not all(self._uploaded(f) for f in files):
            from telethon import errors
            raise errors.ChatForwardsRestrictedError(request=None)
        for f in files:                     # like Telethon: serialize the request (struct.pack)
            if isinstance(f, types.TLObject):
                if self.struct_fail_once:
                    self.struct_fail_once = False
                    import struct
                    struct.pack("<i", 2**31)
                bytes(f)
        self.sent.append(("send_file", dict(kwargs, file=file, entity=entity)))
        if isinstance(file, list):
            return [self._new() for _ in file]
        return self._new()

    async def send_message(self, entity, message, **kwargs):
        self._maybe_fail()
        self.sent.append(("send_message", dict(kwargs, text=message, entity=entity)))
        return self._new()

    async def __call__(self, request):
        self._maybe_fail()
        self.requests.append(request)
        from telethon.tl.functions.messages import ForwardMessagesRequest
        if isinstance(request, ForwardMessagesRequest):
            if self.forward_restricted:
                from telethon import errors
                raise errors.ChatForwardsRestrictedError(request=request)
            ups = [types.UpdateNewChannelMessage(
                message=types.Message(id=self._new().id, peer_id=types.PeerChannel(222), date=None, message=""),
                pts=1, pts_count=1) for _ in request.id]
            return types.Updates(updates=ups, users=[], chats=[], date=None, seq=0)
        raise NotImplementedError(type(request).__name__)

    async def download_media(self, message, file=None, thumb=None, progress_callback=None):
        if progress_callback:
            progress_callback(50, 100)
            progress_callback(100, 100)
        return f"{file}media_{message.id}.bin" if thumb is None else None

    # --- Connection ---
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return True

    def add_event_handler(self, cb, ev):
        self.handlers.append((cb, ev))

    def remove_event_handler(self, cb, ev):
        self.handlers.remove((cb, ev))
