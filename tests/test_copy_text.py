"""Option COPY_TEXT: also copy plain text messages."""

from forwarder.config import load_settings
from tests.fakes import FakeClient, make_msg
from tests.test_engine import make_forwarder, run


def msgs():
    return [make_msg(1, "photo", text="Picture text"), make_msg(2, "text", text="Text only"),
            make_msg(3, "text", text="   "), make_msg(4, "text", text="Im Topic")]


def test_text_skipped_by_default(tmp_path):
    client = FakeClient(msgs())
    fw, _ = make_forwarder(tmp_path, client)
    assert run(fw.run())["total"] == 1
    assert [k for k, _ in client.sent] == ["send_file"]


def test_text_copied_when_enabled(tmp_path):
    client = FakeClient(msgs())
    fw, store = make_forwarder(tmp_path, client, copy_text=True, topic_mode="fixed", target_topic_id=7)
    summary = run(fw.run())
    assert summary["total"] == 3 and summary["ok"] == 3 and summary["failed"] == 0
    kinds = [(k, kw.get("caption") or kw.get("text"), kw["reply_to"]) for k, kw in client.sent]
    assert kinds == [("send_file", "Picture text", 7), ("send_message", "Text only", 7),
                     ("send_message", "Im Topic", 7)]
    assert store.counts()["ok"] == 3


def test_copy_text_env(tmp_path):
    base = {"API_ID": "1", "API_HASH": "x", "DATA_PATH": str(tmp_path)}
    assert load_settings(env=base).copy_text is False
    assert load_settings(env=dict(base, COPY_TEXT="true")).copy_text is True


def test_log_reports_caption_lengths(tmp_path, caplog):
    import logging
    client = FakeClient([make_msg(1, "video", text="Videotext")])
    fw, _ = make_forwarder(tmp_path, client)
    with caplog.at_level(logging.INFO, logger="forwarder.engine"):
        run(fw.run())
    assert any("text source 9 /" in r.getMessage() for r in caplog.records)


def test_parse_ids():
    from forwarder.cli import _parse_ids
    assert _parse_ids(["37-39", "41,43"]) == [37, 38, 39, 41, 43]


def test_filename_placeholder():
    from types import SimpleNamespace
    from forwarder.media import build_caption
    m = SimpleNamespace(message="", id=5, date=None, file=SimpleNamespace(name="clip.mp4"))
    assert build_caption(m, "{filename}") == "clip.mp4"
    assert build_caption(m, "{caption}\n{filename}") == "clip.mp4"
