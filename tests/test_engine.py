"""Tests of the transfer engine with a fake client (no network, no credentials)."""

import asyncio
import threading

from telethon import errors
from telethon.tl import types
from telethon.tl.functions.messages import ForwardMessagesRequest

from forwarder.config import Settings
from forwarder.engine import Forwarder
from forwarder.state import ProgressState
from forwarder.store import Store
from tests.fakes import FakeClient, make_msg


def settings(tmp_path, **kw):
    base = dict(api_id=1, api_hash="x", source_chat="source", target_chat="target",
                delay_seconds=0, data_path=tmp_path)
    base.update(kw)
    return Settings(**base)


def run(coro):
    return asyncio.run(coro)


def make_forwarder(tmp_path, client, **kw):
    store = Store(tmp_path / "db.sqlite")
    fw = Forwarder(client, settings(tmp_path, **kw), store, ProgressState())
    return fw, store


SAMPLE = lambda: [  # noqa: E731
    make_msg(1, "photo", text="Picture 1"),
    make_msg(2, "text"),
    make_msg(3, "video", grouped_id=7),
    make_msg(4, "photo", grouped_id=7),
    make_msg(5, "doc"),
    make_msg(6, "video_file"),
]


def test_copy_history_with_album_and_persistence(tmp_path):
    client = FakeClient(SAMPLE())
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["total"] == 4 and summary["ok"] == 4 and summary["failed"] == 0
    kinds = [(len(k["file"]) if isinstance(k["file"], list) else 1) for _, k in client.sent]
    assert kinds == [1, 2, 1]                                  # single picture, album(2), video file
    assert client.sent[0][1]["caption"] == "Picture 1"
    assert client.sent[0][1]["reply_to"] is None
    assert store.get_checkpoint(fw.source_key) == 6
    assert store.counts()["ok"] == 4

    # second run: nothing left to do (persistence + checkpoint)
    client2 = FakeClient(SAMPLE() + [make_msg(7, "photo")])
    fw2 = Forwarder(client2, fw.settings, store, ProgressState())
    assert run(fw2.run())["total"] == 1
    assert len(client2.sent) == 1


def test_resume_disabled_still_skips_processed(tmp_path):
    client = FakeClient(SAMPLE())
    fw, store = make_forwarder(tmp_path, client)
    run(fw.run())
    fw.settings.resume = False
    client2 = FakeClient(SAMPLE())
    assert run(Forwarder(client2, fw.settings, store, ProgressState()).run())["total"] == 0


def test_forward_mode_uses_raw_request_with_topic(tmp_path):
    client = FakeClient([make_msg(1), make_msg(2, grouped_id=3), make_msg(3, grouped_id=3)])
    fw, store = make_forwarder(tmp_path, client, mode="forward", topic_mode="fixed", target_topic_id=55)
    summary = run(fw.run())
    assert summary["ok"] == 3
    reqs = [r for r in client.requests if isinstance(r, ForwardMessagesRequest)]
    assert [r.id for r in reqs] == [[1], [2, 3]]
    assert all(r.top_msg_id == 55 and r.drop_author for r in reqs)
    assert store.recent(1)[0]["target_ids"]                   # target IDs stored


def test_protected_source_falls_back_to_download(tmp_path):
    client = FakeClient([make_msg(1, "video", noforwards=True), make_msg(2, "photo")], forward_restricted=True)
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["ok"] == 2
    files = [k["file"] for _, k in client.sent]
    assert all("media_" in f.file.name for f in files)                 # re-uploaded
    video = next(a for a in files[0].attributes if isinstance(a, types.DocumentAttributeVideo))
    assert video.supports_streaming is True
    assert isinstance(files[1], types.InputMediaUploadedPhoto)
    assert not list((tmp_path / "tmp").glob("*/*"))                   # temp files cleaned up


def test_protected_without_fallback_marks_failed_and_retries_later(tmp_path):
    client = FakeClient([make_msg(1, "photo", noforwards=True)])
    fw, store = make_forwarder(tmp_path, client, download_fallback=False)
    summary = run(fw.run())
    assert summary["failed"] == 1 and store.failed_ids(fw.source_key) == [1]
    # Later with fallback: failed message is retried (despite the checkpoint)
    fw.settings.download_fallback = True
    summary = run(Forwarder(FakeClient([make_msg(1, "photo", noforwards=True)]), fw.settings,
                            store, ProgressState()).run())
    assert summary["ok"] == 1 and store.failed_ids(fw.source_key) == []


def test_floodwait_is_waited_out_and_retried(tmp_path, monkeypatch):
    flood = errors.FloodWaitError(request=None, capture=2)
    client = FakeClient([make_msg(1)], fail_times=1, fail_exc=flood)
    fw, store = make_forwarder(tmp_path, client)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(fw, "_sleep", fake_sleep)
    summary = run(fw.run())
    assert summary["ok"] == 1 and 3 in slept


def test_connection_errors_exhaust_retries(tmp_path, monkeypatch):
    client = FakeClient([make_msg(1), make_msg(2)], fail_times=3, fail_exc=ConnectionError("weg"))
    fw, store = make_forwarder(tmp_path, client, max_retries=3)

    async def no_sleep(seconds):
        pass
    monkeypatch.setattr(fw, "_sleep", no_sleep)
    summary = run(fw.run())
    assert summary["failed"] == 1 and summary["ok"] == 1     # first given up, second succeeds


def test_dry_run_sends_nothing(tmp_path):
    client = FakeClient(SAMPLE())
    fw, store = make_forwarder(tmp_path, client, dry_run=True)
    summary = run(fw.run())
    assert summary["ok"] == 4 and client.sent == [] and store.counts()["ok"] == 0
    assert store.get_checkpoint(fw.source_key) == 0


def test_stop_event_interrupts(tmp_path):
    client = FakeClient([make_msg(i) for i in range(1, 6)])
    stop = threading.Event()
    store = Store(tmp_path / "db.sqlite")
    fw = Forwarder(client, settings(tmp_path), store, ProgressState(), stop)
    original = fw.process_batch

    async def process_and_stop(batch):
        result = await original(batch)
        stop.set()
        return result
    fw.process_batch = process_and_stop
    summary = run(fw.run())
    assert summary["ok"] == 1 and store.get_checkpoint(fw.source_key) == 1


def test_media_type_and_id_filters(tmp_path):
    client = FakeClient(SAMPLE())
    fw, store = make_forwarder(tmp_path, client, media_types=("video",), start_from_id=2, end_at_id=5)
    summary = run(fw.run())
    assert summary["total"] == 1                               # only video #3 (album part)


def test_live_accepts_filters(tmp_path):
    client = FakeClient([])
    fw, store = make_forwarder(tmp_path, client)
    run(fw.prepare())
    assert fw._live_accepts(make_msg(10))
    assert not fw._live_accepts(make_msg(11, "text"))
    store.mark(fw.source_key, [12], "ok")
    assert not fw._live_accepts(make_msg(12))
