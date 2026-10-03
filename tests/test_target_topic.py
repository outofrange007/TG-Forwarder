"""Target topic ID: validation and link detection (verhindert "'i' format requires …")."""

import pytest

from forwarder.config import ConfigError, Settings, _coerce, load_settings
from forwarder.engine import Aborted
from tests.fakes import FakeClient, make_msg
from tests.test_engine import make_forwarder, run


@pytest.mark.parametrize("value, expected", [
    ("55", 55), (55, 55), ("", None), ("0", None),
    ("https://t.me/c/1234567890/55", 55),
    ("t.me/c/1234567890/55/77", 55),
    ("https://t.me/meinegruppe/12", 12),
])
def test_target_topic_values(value, expected):
    assert _coerce("target_topic_id", value) == expected


@pytest.mark.parametrize("value", ["-1001234567890", "3000000000", "https://t.me/c/123", "abc"])
def test_target_topic_rejects_chat_ids_and_garbage(value):
    with pytest.raises(ConfigError):
        _coerce("target_topic_id", value)


def test_load_settings_rejects_chat_id_as_topic(tmp_path):
    env = {"API_ID": "1", "API_HASH": "x", "DATA_PATH": str(tmp_path),
           "TARGET_TOPIC_ID": "-1001234567890"}
    with pytest.raises(ConfigError, match="chat ID"):
        load_settings(env=env)


def test_engine_aborts_on_invalid_topic_without_sending(tmp_path):
    client = FakeClient([make_msg(1, "video"), make_msg(2, "photo")])
    fw, store = make_forwarder(tmp_path, client, topic_mode="fixed", target_topic_id=1001234567890)
    with pytest.raises(Aborted, match="topic ID"):
        run(fw.run())
    assert client.sent == []
    assert store.counts().get("failed", 0) == 0
