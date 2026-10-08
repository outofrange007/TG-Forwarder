"""Tests: mirror mode uses the real source topic title (no "Topic <id>" placeholders)."""

import logging

from telethon.tl import types

from forwarder import tg_compat
from forwarder.topics import TopicResolver
from tests.fakes import FakeClient, make_msg
from tests.test_engine import make_forwarder, run

SRC, TGT = 111, 222
SK, TK = "-1000000000111", "-1000000000222"   # utils.get_peer_id of the fake channels


def channel(cid, title):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=None,
                         access_hash=cid, megagroup=True, forum=True)


def forum_client(messages, source_topics, target_topics=None, **kw):
    client = FakeClient(messages, source=channel(SRC, "Quelle"), target=channel(TGT, "Ziel"))
    client.forum_topics = {SRC: dict(source_topics), TGT: dict(target_topics or {})}
    for k, v in kw.items():
        setattr(client, k, v)
    return client


def targets(client):
    return {k["reply_to"] for _, k in client.sent}


def test_mirror_creates_target_topic_with_source_title(tmp_path):
    client = forum_client([make_msg(1, topic=101), make_msg(2, topic=102)],
                          {101: "Urlaub 2025", 102: "Katzen 🐱"})
    fw, store = make_forwarder(tmp_path, client, topic_mode="mirror")
    assert run(fw.run())["ok"] == 2
    created = client.forum_topics[TGT]
    assert sorted(created.values()) == ["Katzen 🐱", "Urlaub 2025"]
    assert not any(t.startswith("Topic ") for t in created.values())
    assert {r["title"] for r in store.topics()} == {"Urlaub 2025", "Katzen 🐱"}
    by_id = [r for r in client.requests if type(r).__name__ == "GetForumTopicsByIDRequest"]
    assert by_id and by_id[0].topics == [101]


def test_fallback_to_paginated_listing_when_lookup_by_id_fails(tmp_path, caplog):
    # 150 topics, server only returns 20 per page -> the old loop stopped after page 1
    topics = {i: f"Thema {i}" for i in range(2, 152)}
    client = forum_client([make_msg(1, topic=5)], topics, fail_by_id=True, topics_page_size=20)
    fw, store = make_forwarder(tmp_path, client, topic_mode="mirror")
    with caplog.at_level(logging.WARNING, logger="forwarder.topics"):
        assert run(fw.run())["ok"] == 1
    assert "Thema 5" in client.forum_topics[TGT].values()
    assert "TOPIC_ID_INVALID" in caplog.text                     # reason is logged, not swallowed


def test_list_forum_topics_paginates_by_count():
    topics = {i: f"T{i}" for i in range(2, 152)}
    client = forum_client([], topics, topics_page_size=20)
    result = run(tg_compat.list_forum_topics(client, client.source))
    assert result == topics
    pages = [r for r in client.requests if type(r).__name__ == "GetForumTopicsRequest"]
    assert len(pages) == 8
    assert pages[1].offset_topic == 132 and pages[1].offset_id == 10132   # last topic of page 1


def test_placeholder_only_as_last_resort_with_warning(tmp_path, caplog, monkeypatch):
    async def broken_list(*a, **kw):
        raise ConnectionError("netz weg")
    client = forum_client([make_msg(1, topic=101)], {101: "Urlaub"}, fail_by_id=True)
    fw, _ = make_forwarder(tmp_path, client, topic_mode="mirror")
    run(fw.prepare())
    monkeypatch.setattr(tg_compat, "list_forum_topics", broken_list)
    resolver = TopicResolver(client, fw.settings, fw.store, client.source, client.target, "s", "t")
    with caplog.at_level(logging.WARNING, logger="forwarder.topics"):
        assert run(resolver._source_title(101)) == "Topic 101"
    assert "netz weg" in caplog.text and "placeholder" in caplog.text
    assert run(resolver.lookup_title(1)) == "General"


def test_existing_placeholder_mapping_is_renamed(tmp_path):
    client = forum_client([make_msg(1, topic=101), make_msg(2, topic=101)],
                          {101: "Urlaub"}, {900: "Topic 101"})
    fw, store = make_forwarder(tmp_path, client, topic_mode="mirror")
    store.set_topic(SK, 101, TK, 900, "Topic 101")
    assert run(fw.run())["ok"] == 2
    assert client.forum_topics[TGT] == {900: "Urlaub"}           # renamed, no duplicate
    assert targets(client) == {900}
    assert store.get_topic_mapping(SK, 101, TK) == (900, "Urlaub")
    edits = [r for r in client.requests if type(r).__name__ == "EditForumTopicRequest"]
    assert len(edits) == 1                                       # only once per run


def test_rename_failure_keeps_using_topic(tmp_path, caplog):
    client = forum_client([make_msg(1, topic=101)], {101: "Urlaub"}, {900: "Topic 101"}, fail_edit=True)
    fw, store = make_forwarder(tmp_path, client, topic_mode="mirror")
    store.set_topic(SK, 101, TK, 900, "Topic 101")
    with caplog.at_level(logging.WARNING, logger="forwarder.topics"):
        assert run(fw.run())["ok"] == 1
    assert targets(client) == {900} and client.forum_topics[TGT] == {900: "Topic 101"}
    assert "CHAT_ADMIN_REQUIRED" in caplog.text
    # placeholder stays stored -> a later run (with admin right) retries the rename
    assert store.get_topic_mapping(SK, 101, TK) == (900, "Topic 101")


def test_unmapped_placeholder_topic_in_target_is_reused(tmp_path):
    client = forum_client([make_msg(1, topic=101)], {101: "Urlaub"}, {900: "Topic 101"})
    fw, store = make_forwarder(tmp_path, client, topic_mode="mirror")
    assert run(fw.run())["ok"] == 1
    assert client.forum_topics[TGT] == {900: "Urlaub"} and targets(client) == {900}
    assert store.get_topic_mapping(SK, 101, TK) == (900, "Urlaub")


def test_source_topic_selection_uses_lookup_by_id(tmp_path):
    topics = {i: f"Thema {i}" for i in range(2, 152)}
    client = forum_client([make_msg(1, topic=5)], topics, topics_page_size=20)
    fw, _ = make_forwarder(tmp_path, client, source_topic_id=5)
    run(fw.prepare())
    assert fw.source_topic_title == "Thema 5"
