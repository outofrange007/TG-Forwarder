"""Tests: topic mirroring and web API (fake worker, without Telegram)."""

import asyncio
from types import SimpleNamespace

import pytest

from forwarder import tg_compat
from forwarder.config import load_settings
from forwarder.state import ProgressState
from forwarder.store import Store
from forwarder.topics import TopicResolver
from forwarder.web import create_app
from tests.fakes import make_msg


# ---------------------------------------------------------------- Topics
def test_mirror_topics_reuse_and_create(tmp_path, monkeypatch):
    created = []
    src = SimpleNamespace(forum=True, name="SRC")
    tgt = SimpleNamespace(forum=True, name="TGT")

    async def fake_list(client, entity, limit=100):
        return {5: "Urlaub", 6: "Katzen"} if entity is src else {900: "urlaub "}

    async def fake_create(client, entity, title):
        assert entity is tgt
        created.append(title)
        return 901

    monkeypatch.setattr(tg_compat, "list_forum_topics", fake_list)
    monkeypatch.setattr(tg_compat, "create_forum_topic", fake_create)
    settings = SimpleNamespace(topic_mode="mirror", target_topic_id=None, dry_run=False,
                               source_chat="s", target_chat="t")
    store = Store(tmp_path / "db.sqlite")
    r = TopicResolver(None, settings, store, src, tgt, "-1001", "-1002")

    async def scenario():
        return (await r.resolve(make_msg(1, topic=5)),   # exists in the target (spelling irrelevant)
                await r.resolve(make_msg(2, topic=6)),   # gets created
                await r.resolve(make_msg(3, topic=6)),   # from the database
                await r.resolve(make_msg(4)))            # General -> no topic
    assert asyncio.run(scenario()) == (900, 901, 901, None)
    assert created == ["Katzen"]
    assert store.get_topic("-1001", 6, "-1002") == 901


def test_fixed_and_none_topic_modes(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    fixed = TopicResolver(None, SimpleNamespace(topic_mode="fixed", target_topic_id=42,
                                                source_chat="s", target_chat="t"), store)
    none = TopicResolver(None, SimpleNamespace(topic_mode="none", target_topic_id=42,
                                               source_chat="s", target_chat="t"), store)
    assert asyncio.run(fixed.resolve(make_msg(1))) == 42
    assert asyncio.run(none.resolve(make_msg(1))) is None


def test_new_message_ids_from_updates():
    from telethon.tl import types
    upd = types.Updates(updates=[
        types.UpdateNewChannelMessage(message=types.Message(id=77, peer_id=types.PeerChannel(1), date=None, message=""),
                                      pts=1, pts_count=1),
        types.UpdateMessageID(id=77, random_id=1),
    ], users=[], chats=[], date=None, seq=0)
    assert tg_compat.new_message_ids(upd) == [77]


# ------------------------------------------------------------------- Web
class FakeWorker:
    def __init__(self):
        self.running = False
        self.started = None

    def call(self, coro, timeout=60):
        return asyncio.run(coro)

    async def auth_status(self):
        return {"authorized": True, "user": {"name": "Test", "username": "t"}}

    async def send_code(self, phone):
        return {"success": True}

    async def sign_in(self, code):
        return {"success": False, "password_required": True}

    async def sign_in_password(self, pw):
        return {"success": pw == "geheim"}

    async def dialogs(self):
        return [{"id": -100123, "title": "Group"}]

    def start_job(self, live=False):
        if self.running:
            raise RuntimeError("A run is already in progress")
        self.running, self.started = True, live

    def stop_job(self):
        self.running = False


@pytest.fixture
def web(tmp_path):
    settings = load_settings(env={"API_ID": "1", "API_HASH": "geheimer_hash", "DATA_PATH": str(tmp_path)})
    store = Store(settings.db_path)
    worker = FakeWorker()
    app = create_app(settings, store, ProgressState(), worker)
    return app.test_client(), settings, worker


def test_web_index_and_status(web):
    client, settings, worker = web
    assert "Telegram Forwarder" in client.get("/").get_data(as_text=True)
    status = client.get("/api/status").get_json()
    assert status["running"] is False and status["totals"]["ok"] == 0
    auth = client.get("/api/auth/status").get_json()
    assert auth["authorized"] and auth["configured"]


def test_web_login_flow(web):
    client, *_ = web
    assert client.post("/api/auth/send_code", json={"phone": "0170"}).status_code == 400
    assert client.post("/api/auth/send_code", json={"phone": "+49 170 1234567"}).get_json()["success"]
    assert client.post("/api/auth/sign_in", json={"code": "12345"}).get_json()["password_required"]
    assert client.post("/api/auth/password", json={"password": "geheim"}).get_json()["success"]


def test_web_settings_never_expose_secrets_and_persist(web):
    client, settings, worker = web
    data = client.get("/api/settings").get_json()
    assert "geheimer_hash" not in str(data)
    res = client.post("/api/settings", json={"source_chat": "https://t.me/c/999/1", "target_chat": "@target",
                                             "media_types": "photo,video", "api_hash": "boese"})
    assert res.get_json()["success"]
    assert settings.source_chat == -100999 and settings.api_hash == "geheimer_hash"
    assert '"source_chat": -100999' in settings.overrides_path.read_text()
    bad = client.post("/api/settings", json={"mode": "quatsch"})
    assert bad.status_code == 400 and settings.mode == "copy"   # rollback on error


def test_web_start_stop(web):
    client, settings, worker = web
    assert client.post("/api/start", json={"live": True}).get_json()["success"] and worker.started is True
    second = client.post("/api/start", json={})
    assert second.status_code == 400 and "already" in second.get_json()["error"]
    assert client.post("/api/settings", json={"mode": "forward"}).status_code == 400  # locked
    client.post("/api/stop")
    assert worker.running is False
    assert client.get("/api/chats").get_json()["chats"][0]["id"] == -100123


def test_web_password_protection(tmp_path):
    settings = load_settings(env={"API_ID": "1", "API_HASH": "x", "DATA_PATH": str(tmp_path),
                                  "WEB_PASSWORD": "pw123"})
    app = create_app(settings, Store(settings.db_path), ProgressState(), FakeWorker()).test_client()
    assert app.get("/api/status").status_code == 401
    import base64
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw123").decode()}
    assert app.get("/api/status", headers=hdr).status_code == 200
