"""Expired file references: re-fetch the batch's messages once and retry once."""

import logging

from tests.fakes import FakeClient, make_msg
from tests.test_engine import make_forwarder, run


def stale(msg):
    """Message object whose file reference has expired (media and download fail)."""
    msg.file_ref_expired = True
    msg.media.file_ref_expired = True
    return msg


def test_reupload_path_succeeds_after_refetch(tmp_path, caplog):
    old = stale(make_msg(4457, "video", noforwards=True))
    old.document.thumbs = [object()]                       # thumb download is affected too
    client = FakeClient([old], forward_restricted=True)
    client.refetch = {4457: make_msg(4457, "video", noforwards=True)}
    fw, store = make_forwarder(tmp_path, client)
    with caplog.at_level(logging.INFO, logger="forwarder.engine"):
        summary = run(fw.run())
    assert summary["ok"] == 1 and summary["failed"] == 0
    assert client.refetch_calls == [[4457]]
    assert "File reference expired for" in caplog.text and "retrying once" in caplog.text
    assert store.failed_ids(fw.source_key) == []


def test_direct_path_refetch_then_success_album(tmp_path):
    msgs = [stale(make_msg(1, "photo", grouped_id=9)), stale(make_msg(2, "video", grouped_id=9))]
    client = FakeClient(msgs)
    client.refetch = {1: make_msg(1, "photo", grouped_id=9), 2: make_msg(2, "video", grouped_id=9)}
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["ok"] == 2 and summary["failed"] == 0
    assert client.refetch_calls == [[1, 2]]
    assert not getattr(client, "downloads", [])            # resent by reference, no download fallback
    sent = client.sent[0][1]["file"]
    assert isinstance(sent, list) and len(sent) == 2
    assert not any(getattr(f, "file_ref_expired", False) for f in sent)


def test_second_expiry_marks_failed(tmp_path):
    client = FakeClient([stale(make_msg(5, "photo"))])
    client.refetch = {5: stale(make_msg(5, "photo"))}      # still stale after refetch
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["failed"] == 1 and summary["ok"] == 0
    assert client.refetch_calls == [[5]]                   # exactly once, no recursion
    assert store.failed_ids(fw.source_key) == [5]


def test_refetch_returns_none_marks_failed(tmp_path):
    client = FakeClient([stale(make_msg(6, "video", noforwards=True))], forward_restricted=True)
    client.refetch = {6: None}                             # deleted in the source meanwhile
    fw, store = make_forwarder(tmp_path, client)
    summary = run(fw.run())
    assert summary["failed"] == 1
    assert client.refetch_calls == [[6]]
    assert store.failed_ids(fw.source_key) == [6]
    assert "no longer available" in store.recent(1)[0]["info"]
