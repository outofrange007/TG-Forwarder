"""Tests: 32-bit metadata errors, upload size limits and fallback when re-uploading."""

import math
import struct

import pytest
from telethon import utils
from telethon.tl import types
from telethon.tl.functions.messages import SendMediaRequest

from forwarder.config import ConfigError, load_settings
from forwarder.engine import describe_error
from forwarder.upload_meta import (
    MB, FileTooLargeError, check_upload_size, find_invalid_values, sanitize_attributes,
)
from tests.fakes import FakeClient, make_msg
from tests.test_engine import make_forwarder, run


def broken_video_attr():
    return types.DocumentAttributeVideo(duration=float("nan"), w=2**32 - 1, h=-5,
                                        preload_prefix_size=2**40, supports_streaming=True)


def test_root_cause_reproduced_with_telethon():
    """Exactly the message from the dashboard: width > 2^31 in DocumentAttributeVideo."""
    with pytest.raises(struct.error, match="'i' format requires -2147483648 <= number <= 2147483647"):
        bytes(types.DocumentAttributeVideo(duration=1.0, w=2**32 - 1, h=720))


def test_sanitize_clamps_and_keeps_original():
    original = broken_video_attr()
    name = types.DocumentAttributeFilename("ерер (11).mp4")
    fixed = sanitize_attributes([original, name, types.DocumentAttributeImageSize(w=2**31, h=10)])
    video, fname, size = fixed
    assert (video.w, video.h, video.duration, video.preload_prefix_size) == (1, 1, 0.0, None)
    assert video.supports_streaming is True and fname.file_name == "ерер (11).mp4"
    assert (size.w, size.h) == (1, 10)
    assert original.w == 2**32 - 1 and math.isnan(original.duration)   # original unchanged
    for a in fixed:
        bytes(a)


def test_sanitize_keeps_valid_values():
    ok = types.DocumentAttributeVideo(duration=12.5, w=1920, h=1080)
    assert vars(sanitize_attributes([ok])[0]) == vars(ok)


def test_find_invalid_values_names_field():
    media = types.InputMediaUploadedDocument(
        file=types.InputFile(id=1, parts=1, name="a.mp4", md5_checksum=""), mime_type="video/mp4",
        attributes=[types.DocumentAttributeVideo(duration=1.0, w=2**32 - 1, h=720)])
    assert find_invalid_values([media]) == [
        "[0].InputMediaUploadedDocument.attributes[0].DocumentAttributeVideo.w=4294967295"]


def test_sanitized_media_serializes_in_real_request():
    media = types.InputMediaUploadedDocument(
        file=types.InputFileBig(id=1, parts=4500, name="gross.mp4"), mime_type="video/mp4",
        attributes=sanitize_attributes([broken_video_attr()]))
    bytes(SendMediaRequest(peer=types.InputPeerSelf(), media=media, message="x",
                           reply_to=types.InputReplyToMessage(reply_to_msg_id=5, top_msg_id=5)))


def test_upload_size_limits():
    check_upload_size(2000 * MB, premium=False, name="a.mp4")
    check_upload_size(None, premium=False, name="a.mp4")
    with pytest.raises(FileTooLargeError, match="at most 2000 MB.*up to 4000 MB"):
        check_upload_size(2100 * MB, premium=False, name="a.mp4")
    check_upload_size(3900 * MB, premium=True, name="a.mp4")
    with pytest.raises(FileTooLargeError, match="at most 4000 MB"):
        check_upload_size(4100 * MB, premium=True, name="a.mp4")


def test_describe_error_struct():
    try:
        struct.pack("<i", 2**31)
    except struct.error as exc:
        msg = describe_error(exc)
    assert "32-bit field" in msg and "metadata" in msg


def test_api_id_out_of_range_rejected(tmp_path):
    s = load_settings(env={"API_ID": "3000000000", "API_HASH": "x", "DATA_PATH": str(tmp_path)})
    with pytest.raises(ConfigError, match="API_ID is invalid"):
        s.validate(require_chats=False)


# ------------------------------------------------------------- Engine
def protected_video(mid=1, size=1000, attrs=None, grouped_id=None):
    m = make_msg(mid, "video", noforwards=True, size=size, name="ерер (11).mp4", grouped_id=grouped_id)
    m.document.attributes = attrs if attrs is not None else [broken_video_attr()]
    return m


def sent_media(client):
    return [k["file"] for _, k in client.sent]


def test_reupload_with_broken_metadata_succeeds(tmp_path):
    client = FakeClient([protected_video()])
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["ok"] == 1 and summary["failed"] == 0
    video = next(a for a in sent_media(client)[0].attributes if isinstance(a, types.DocumentAttributeVideo))
    assert (video.w, video.h, video.duration) == (1, 1, 0.0)


def test_hachoir_crash_does_not_block_upload(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("broken file")
    monkeypatch.setattr(utils, "get_attributes", boom)
    client = FakeClient([protected_video(attrs=[types.DocumentAttributeVideo(duration=3.0, w=640, h=360)])])
    fw, _ = make_forwarder(tmp_path, client)
    assert run(fw.run())["ok"] == 1
    video = next(a for a in sent_media(client)[0].attributes if isinstance(a, types.DocumentAttributeVideo))
    assert (video.w, video.h) == (640, 360)


def test_struct_error_falls_back_to_minimal_attributes(tmp_path):
    client = FakeClient([protected_video(attrs=[])], struct_fail_once=True)
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["ok"] == 1
    media = sent_media(client)[0]
    assert media.thumb is None
    assert [type(a).__name__ for a in media.attributes] == ["DocumentAttributeFilename", "DocumentAttributeVideo"]
    assert len(client.uploads) == 1                                    # not uploaded twice
    assert any("without attributes" in e["text"] for e in fw.state.snapshot()["events"])


def test_too_large_file_clear_message_without_download(tmp_path):
    client = FakeClient([protected_video(size=2500 * MB)])
    fw, store = make_forwarder(tmp_path, client, max_file_size_mb=5000)
    summary = run(fw.run())
    assert summary["failed"] == 1 and client.uploads == [] and client.sent == []
    events = " ".join(e["text"] for e in fw.state.snapshot()["events"])
    assert "Telegram allows at most 2000 MB" in events
    assert "format requires" not in events

    premium = FakeClient([protected_video(size=2500 * MB)], premium=True)
    fw2, _ = make_forwarder(tmp_path / "p", premium, max_file_size_mb=5000)
    assert run(fw2.run())["ok"] == 1


def test_protected_album_uses_sanitized_media(tmp_path):
    photo = make_msg(2, "photo", noforwards=True, grouped_id=5)
    client = FakeClient([protected_video(grouped_id=5), photo])
    fw, _ = make_forwarder(tmp_path, client)
    assert run(fw.run())["ok"] == 2
    album = sent_media(client)
    assert len(album) == 1 and len(album[0]) == 2
    assert isinstance(album[0][0], types.InputMediaUploadedDocument)
    assert isinstance(album[0][1], types.InputMediaUploadedPhoto)
    assert all(up[1] for up in client.uploads)                          # progress per file
