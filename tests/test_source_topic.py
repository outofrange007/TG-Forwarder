"""Tests: source topic selection (SOURCE_TOPIC_ID) - config, checkpoints, engine, web, CLI."""

from types import SimpleNamespace

import pytest
from telethon.tl import types

from forwarder import tg_compat
from forwarder.cli import build_parser
from forwarder.config import ConfigError, load_settings, parse_topic_id
from forwarder.engine import Forwarder, list_source_topics
from forwarder.state import ProgressState
from forwarder.store import Store
from tests.fakes import FakeClient, make_msg
from tests.test_engine import make_forwarder, run
from tests.test_topics_web import FakeWorker, web  # noqa: F401  (fixture)

TOPICS = {1: "General", 5: "Urlaub", 6: "Katzen"}


def forum_source():
    return types.Channel(id=111, title="Forum", photo=types.ChatPhotoEmpty(), date=None,
                         access_hash=1, megagroup=True, forum=True)


@pytest.fixture
def forum_topics(monkeypatch):
    async def fake_list(client, entity, limit=100):
        return dict(TOPICS) if getattr(entity, "forum", False) else {}
    monkeypatch.setattr(tg_compat, "list_forum_topics", fake_list)


def forum_messages():
    return [
        make_msg(1, "photo"),                          # General (no reply_to)
        make_msg(2, "photo", topic=5),
        make_msg(3, "video", grouped_id=9, topic=5),   # album in topic 5
        make_msg(4, "photo", grouped_id=9, topic=5),
        make_msg(5, "photo", topic=6),
        make_msg(6, "text", topic=5),
        make_msg(7, "video", topic=6),
    ]


def sent_count(client):
    return sum(len(k["file"]) if isinstance(k["file"], list) else 1 for _, k in client.sent)


# ---------------------------------------------------------------- Config
@pytest.mark.parametrize("value", [None, "", "  ", "0", "all", "ALLE", "*"])
def test_parse_topic_id_all(value):
    assert parse_topic_id(value) is None


def test_parse_topic_id_values_and_errors():
    assert parse_topic_id("5") == 5 and parse_topic_id(7) == 7 and parse_topic_id(" 1 ") == 1
    with pytest.raises(ConfigError):
        parse_topic_id("-3")
    with pytest.raises(ConfigError):
        parse_topic_id("abc")


def test_load_settings_source_topic(tmp_path):
    base = {"API_ID": "1", "API_HASH": "x", "DATA_PATH": str(tmp_path)}
    assert load_settings(env=dict(base, SOURCE_TOPIC_ID="all")).source_topic_id is None
    assert load_settings(env=dict(base, SOURCE_TOPIC_ID="12")).source_topic_id == 12
    assert load_settings(env=base).source_topic_id is None


# ----------------------------------------------------------- Checkpoints
def test_checkpoints_per_topic_and_reset(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert Store.checkpoint_key("-1001") == "checkpoint:-1001"
    assert Store.checkpoint_key("-1001", 5) == "checkpoint:-1001:topic:5"
    store.set_checkpoint("-1001", 10)
    store.set_checkpoint("-1001", 50, 5)
    store.set_checkpoint("-1002", 70, 5)
    assert store.get_checkpoint("-1001") == 10
    assert store.get_checkpoint("-1001", 5) == 50
    assert store.get_checkpoint("-1001", 6) == 0
    store.reset("-1001")
    assert store.get_checkpoint("-1001") == 0 and store.get_checkpoint("-1001", 5) == 0
    assert store.get_checkpoint("-1002", 5) == 70          # other source untouched


# ---------------------------------------------------------------- Engine
def test_history_only_selected_topic_incl_album(tmp_path, forum_topics):
    client = FakeClient(forum_messages(), source=forum_source())
    fw, store = make_forwarder(tmp_path, client, source_topic_id=5)
    summary = run(fw.run())
    assert summary["total"] == 3 and summary["ok"] == 3
    assert [len(k["file"]) if isinstance(k["file"], list) else 1 for _, k in client.sent] == [1, 2]
    assert fw.source_topic_title == "Urlaub"
    assert store.get_checkpoint(fw.source_key, 5) == 7
    assert store.get_checkpoint(fw.source_key) == 0          # global checkpoint untouched

    # then "all topics": remaining media, no duplicates
    client2 = FakeClient(forum_messages(), source=forum_source())
    fw2 = Forwarder(client2, fw.settings.__class__(**{**vars(fw.settings), "source_topic_id": None}),
                    store, ProgressState())
    summary2 = run(fw2.run())
    assert summary2["total"] == 3 and sent_count(client2) == 3   # #1 (General), #5, #7
    assert store.get_checkpoint(fw.source_key) == 7


def test_topic_switch_does_not_reuse_other_checkpoint(tmp_path, forum_topics):
    client = FakeClient(forum_messages(), source=forum_source())
    fw, store = make_forwarder(tmp_path, client, source_topic_id=5)
    run(fw.run())
    client2 = FakeClient(forum_messages(), source=forum_source())
    fw2 = Forwarder(client2, fw.settings.__class__(**{**vars(fw.settings), "source_topic_id": 6}),
                    store, ProgressState())
    assert run(fw2.run())["total"] == 2                      # #5, #7 despite checkpoint 7 in topic 5
    assert store.get_checkpoint(fw.source_key, 6) == 7


def test_general_topic_captures_messages_without_reply_to(tmp_path, forum_topics):
    seen = {}
    client = FakeClient(forum_messages(), source=forum_source())
    original = client.iter_messages

    def spy(entity, **kw):
        seen["reply_to"] = kw.get("reply_to")
        return original(entity, **kw)
    client.iter_messages = spy
    fw, store = make_forwarder(tmp_path, client, source_topic_id=1)
    assert run(fw.run())["total"] == 1
    assert seen["reply_to"] is None                          # General: filtered client-side

    client2 = FakeClient(forum_messages(), source=forum_source())
    original2 = client2.iter_messages
    client2.iter_messages = lambda entity, **kw: seen.update(reply_to=kw.get("reply_to")) or original2(entity, **kw)
    fw2, _ = make_forwarder(tmp_path / "b", client2, source_topic_id=6)
    run(fw2.run())
    assert seen["reply_to"] == 6                             # real thread: server-side


def test_failed_retry_respects_topic(tmp_path, forum_topics):
    client = FakeClient(forum_messages(), source=forum_source())
    fw, store = make_forwarder(tmp_path, client, source_topic_id=5)
    run(fw.prepare())
    store.mark(fw.source_key, [5], "failed", info="x")       # belongs to topic 6
    store.mark(fw.source_key, [2], "failed", info="x")       # belongs to topic 5
    store.set_checkpoint(fw.source_key, 7, 5)
    ids = [m.id for m in run(fw.collect_candidates())]
    assert ids == [2]


def test_prepare_errors(tmp_path, forum_topics):
    fw, _ = make_forwarder(tmp_path, FakeClient([]), source_topic_id=5)       # not a forum
    with pytest.raises(ValueError, match="not a forum"):
        run(fw.prepare())
    fw2, _ = make_forwarder(tmp_path / "b", FakeClient([], source=forum_source()), source_topic_id=42)
    with pytest.raises(ValueError, match="does not exist"):
        run(fw2.prepare())


def test_live_accepts_topic_filter(tmp_path, forum_topics):
    fw, store = make_forwarder(tmp_path, FakeClient([], source=forum_source()), source_topic_id=5)
    run(fw.prepare())
    assert fw._live_accepts(make_msg(10, topic=5))
    assert not fw._live_accepts(make_msg(11, topic=6))
    assert not fw._live_accepts(make_msg(12))                # General
    top = make_msg(13)
    top.reply_to = SimpleNamespace(forum_topic=True, reply_to_msg_id=99, reply_to_top_id=5)
    assert fw._live_accepts(top)                             # reply within the topic


def test_mirror_only_selected_topic(tmp_path, monkeypatch):
    created = []

    async def fake_list(client, entity, limit=100):
        return dict(TOPICS) if entity.id == 111 else {}

    async def fake_create(client, entity, title):
        created.append(title)
        return 500 + len(created)

    monkeypatch.setattr(tg_compat, "list_forum_topics", fake_list)
    monkeypatch.setattr(tg_compat, "create_forum_topic", fake_create)
    target = types.Channel(id=222, title="Target", photo=types.ChatPhotoEmpty(), date=None,
                           access_hash=2, megagroup=True, forum=True)
    client = FakeClient(forum_messages(), source=forum_source(), target=target)
    fw, store = make_forwarder(tmp_path, client, source_topic_id=6, topic_mode="mirror")
    assert run(fw.run())["total"] == 2
    assert created == ["Katzen"]
    assert {k["reply_to"] for _, k in client.sent} == {501}


def test_list_source_topics(forum_topics):
    info = run(list_source_topics(FakeClient([], source=forum_source()), "source"))
    assert info["forum"] and info["title"] == "Forum"
    assert info["topics"][1] == {"id": 5, "title": "Urlaub"}
    plain = run(list_source_topics(FakeClient([]), "source"))
    assert plain["forum"] is False and plain["topics"] == []


# ------------------------------------------------------------------- Web
async def _fake_source_topics(self, chat=None):
    return {"chat_id": -100111, "title": "Forum", "forum": True, "chat": chat,
            "topics": [{"id": 5, "title": "Urlaub"}]}


def test_web_source_topics_and_settings(web, monkeypatch):  # noqa: F811
    monkeypatch.setattr(FakeWorker, "source_topics", _fake_source_topics, raising=False)
    client, settings, worker = web
    data = client.get("/api/source_topics?chat=https://t.me/c/111/5").get_json()
    assert data["topics"][0]["id"] == 5 and data["chat"] == -100111 and data["selected"] is None
    assert client.post("/api/settings", json={"source_topic_id": "5"}).get_json()["success"]
    assert settings.source_topic_id == 5
    assert client.get("/api/source_topics").get_json()["selected"] == 5
    assert client.post("/api/settings", json={"source_topic_id": ""}).get_json()["success"]
    assert settings.source_topic_id is None
    assert client.post("/api/settings", json={"source_topic_id": "-2"}).status_code == 400


# ------------------------------------------------------------------- CLI
def test_cli_source_topic_and_topics_command():
    p = build_parser()
    assert p.parse_args(["run", "--source-topic", "all"]).source_topic == "all"
    assert p.parse_args(["run"]).source_topic is None
    assert p.parse_args(["topics", "--chat", "@gruppe"]).chat == "@gruppe"
