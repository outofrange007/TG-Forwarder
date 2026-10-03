"""Unit tests for configuration, media logic, persistence and progress."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from forwarder.config import ConfigError, Settings, load_settings, parse_chat_ref, parse_media_types
from forwarder.media import build_caption, check_message, classify, group_batches, source_topic_id
from forwarder.state import ProgressState
from forwarder.store import STATUS_FAILED, STATUS_OK, STATUS_SKIPPED, Store
from tests.fakes import make_msg


# ---------------------------------------------------------------- config
@pytest.mark.parametrize("raw,expected", [
    ("-1001234567890", -1001234567890),
    ("@kanal_name", "@kanal_name"),
    ("kanal_name", "@kanal_name"),
    ("https://t.me/kanal_name", "@kanal_name"),
    ("t.me/kanal_name/123", "@kanal_name"),
    ("https://t.me/c/1234567890/55", -1001234567890),
    ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123"),
    ("", None),
])
def test_parse_chat_ref(raw, expected):
    assert parse_chat_ref(raw) == expected


def test_parse_chat_ref_invalid():
    with pytest.raises(ConfigError):
        parse_chat_ref("invalid!!")


def test_parse_media_types():
    assert parse_media_types("Photo, video") == ("photo", "video")
    assert "video_note" in parse_media_types("all")
    with pytest.raises(ConfigError):
        parse_media_types("audio")


def test_load_settings_from_env(tmp_path):
    env = {"API_ID": "123", "API_HASH": "abc", "SOURCE_CHAT": "@source", "TARGET_CHAT": "-100222",
           "TARGET_TOPIC_ID": "7", "DATA_PATH": str(tmp_path), "CAPTION_TEMPLATE": "{caption}\\n#tag",
           "HIDE_SENDER": "nein", "DATE_FROM": "2026-01-01"}
    s = load_settings(env=env)
    assert s.api_id == 123 and s.source_chat == "@source" and s.target_chat == -100222
    assert s.topic_mode == "fixed"          # derived automatically
    assert s.caption_template == "{caption}\n#tag"
    assert s.hide_sender is False
    assert s.date_from.tzinfo is not None
    s.validate()
    assert "api_hash" not in s.public_dict() and s.public_dict()["api_hash_set"] is True


def test_validate_errors(tmp_path):
    s = load_settings(env={"DATA_PATH": str(tmp_path), "MODE": "bogus"})
    with pytest.raises(ConfigError) as exc:
        s.validate()
    msg = str(exc.value)
    assert "API_ID" in msg and "SOURCE_CHAT" in msg and "MODE" in msg


def test_overrides_roundtrip(tmp_path):
    env = {"API_ID": "1", "API_HASH": "x", "DATA_PATH": str(tmp_path)}
    s = load_settings(env=env)
    s.apply_overrides({"source_chat": "https://t.me/c/555/1", "media_types": "photo", "delay_seconds": "0.5"})
    s.save_overrides({"source_chat": s.source_chat, "media_types": s.media_types, "delay_seconds": 0.5})
    s2 = load_settings(env=env)
    assert s2.source_chat == -100555 and s2.media_types == ("photo",) and s2.delay_seconds == 0.5
    with pytest.raises(ConfigError):
        s2.apply_overrides({"api_hash": "hack"})


# ----------------------------------------------------------------- media
@pytest.mark.parametrize("kind,expected", [
    ("photo", "photo"), ("video", "video"), ("animation", "animation"), ("video_note", "video_note"),
    ("image_file", "image_file"), ("video_file", "video_file"), ("doc", None), ("text", None),
])
def test_classify(kind, expected):
    assert classify(make_msg(1, kind)) == expected


def test_check_message_filters():
    s = Settings(media_types=("photo", "video"), start_from_id=10, end_at_id=100, max_file_size_mb=1,
                 date_from=datetime(2025, 6, 1, tzinfo=timezone.utc))
    assert check_message(make_msg(50, "photo"), s).accepted
    assert not check_message(make_msg(5, "photo"), s).accepted                    # too old (ID)
    assert not check_message(make_msg(500, "photo"), s).accepted                  # after END_AT_ID
    assert not check_message(make_msg(50, "animation"), s).accepted               # type not selected
    assert not check_message(make_msg(50, "video", size=5 * 1024 * 1024), s).accepted  # too large
    assert not check_message(make_msg(50, "photo", date=datetime(2024, 1, 1, tzinfo=timezone.utc)), s).accepted
    assert check_message(make_msg(50, "text"), s).reason == "no photo/video"


def test_group_batches_albums_and_order():
    msgs = [make_msg(5, grouped_id=9), make_msg(1), make_msg(4, grouped_id=9), make_msg(6), make_msg(3, grouped_id=9)]
    batches = group_batches(msgs)
    assert [b.ids for b in batches] == [[1], [3, 4, 5], [6]]
    assert batches[1].is_album and batches[1].max_id == 5


def test_group_batches_splits_large_albums():
    msgs = [make_msg(i, grouped_id=1) for i in range(1, 13)]
    assert [len(b.messages) for b in group_batches(msgs)] == [10, 2]


def test_build_caption():
    m = make_msg(42, text="Hallo", date=datetime(2026, 3, 4, 5, 6, tzinfo=timezone.utc))
    assert build_caption(m, "{caption}") == "Hallo"
    assert build_caption(m, "") == ""
    assert build_caption(m, "{caption}\n\nFrom {source} #{id} {date} {unknown}", "channel") == \
        "Hallo\n\nFrom channel #42 2026-03-04 05:06 {unknown}"
    assert build_caption(m, "kaputt { {caption}") == "kaputt { Hallo"
    assert len(build_caption(make_msg(1, text="x" * 2000), "{caption}")) == 1024


def test_source_topic_id():
    assert source_topic_id(make_msg(1, topic=77)) == 77
    assert source_topic_id(make_msg(1)) == 1                  # General
    assert source_topic_id(make_msg(1), source_is_forum=False) is None
    reply_in_topic = make_msg(2)
    reply_in_topic.reply_to = SimpleNamespace(forum_topic=True, reply_to_msg_id=99, reply_to_top_id=77)
    assert source_topic_id(reply_in_topic) == 77


# ----------------------------------------------------------------- store
def test_store_processed_and_checkpoint(tmp_path):
    st = Store(tmp_path / "db.sqlite")
    st.mark("-100111", [1, 2], STATUS_OK, target_ids=[10, 11])
    st.mark("-100111", [3], STATUS_FAILED, info="boom")
    st.mark("-100111", [4], STATUS_SKIPPED)
    assert st.is_processed("-100111", 1) and st.is_processed("-100111", 4)
    assert not st.is_processed("-100111", 3) and not st.is_processed("-100999", 1)
    assert st.failed_ids("-100111") == [3]
    st.mark("-100111", [3], STATUS_OK)                        # Upsert
    assert st.failed_ids("-100111") == []
    assert st.counts() == {"ok": 3, "failed": 0, "skipped": 1}
    st.set_checkpoint("-100111", 50)
    st.set_checkpoint("-100111", 20)                           # never backwards
    assert st.get_checkpoint("-100111") == 50
    st.set_topic("-100111", 5, "-100222", 900, "Urlaub")
    assert st.get_topic("-100111", 5, "-100222") == 900
    st.reset()
    assert st.counts()["ok"] == 0 and st.get_checkpoint("-100111") == 0
    assert st.get_topic("-100111", 5, "-100222") == 900       # topics are kept without include_topics
    st.close()
    assert Store(tmp_path / "db.sqlite").get_topic("-100111", 5, "-100222") == 900  # persistent


# ----------------------------------------------------------------- state
def test_progress_callback_speed_and_snapshot():
    t = [100.0]
    state = ProgressState(clock=lambda: t[0])
    state.reset_job("history")
    state.update(total=4)
    cb = state.progress_callback("Upload", interval=0.5)
    t[0] += 1.0
    cb(2 * 1024 * 1024, 4 * 1024 * 1024)
    snap = state.snapshot()
    assert snap["file_progress"] == 50.0 and snap["speed_mbps"] == 2.0 and snap["file_size_mb"] == 4.0
    state.item_finished("ok")
    state.item_finished("failed")
    t[0] += 10
    snap = state.snapshot()
    assert snap["done"] == 2 and snap["ok"] == 1 and snap["failed"] == 1
    assert snap["overall_progress"] == 50.0 and snap["eta"] == "00:00:11"


def test_progress_callback_count_mode():
    state = ProgressState()
    state.progress_callback("Upload", count_mode=True, interval=0)(1.5, 3)
    snap = state.snapshot()
    assert snap["file_progress"] == 50.0 and snap["file_size_mb"] == 0.0
