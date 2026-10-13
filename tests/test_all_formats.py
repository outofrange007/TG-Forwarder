"""All file formats (audio, voice, sticker, document) and IGNORE_HISTORY."""
from types import SimpleNamespace

from forwarder.config import ALL_MEDIA_TYPES, load_settings, parse_media_types
from forwarder.engine import Forwarder
from forwarder.media import check_message, classify
from forwarder.state import ProgressState
from forwarder.store import Store
from tests.fakes import FakeClient, make_msg
from tests.test_engine import settings


def _doc(mid, mime, **flags):
    m = make_msg(mid, "doc", mime=mime)
    m.document.mime_type = mime
    for k, v in flags.items():
        setattr(m, k, v)
    return m


def test_classify_all_formats():
    assert classify(_doc(1, "application/zip")) == "document"
    assert classify(_doc(2, "application/pdf")) == "document"
    assert classify(_doc(3, "audio/mpeg", audio=object())) == "audio"
    assert classify(_doc(4, "audio/ogg", voice=object())) == "voice"
    assert classify(_doc(5, "image/webp", sticker=object())) == "sticker"
    assert classify(_doc(6, "image/webp")) == "sticker"
    assert classify(_doc(7, "image/png")) == "image_file"


def test_all_includes_every_format():
    types = parse_media_types("all")
    assert types == ALL_MEDIA_TYPES
    for t in ("audio", "voice", "sticker", "document"):
        assert t in types


def test_document_filtered_unless_selected():
    m = _doc(10, "application/pdf")
    assert not check_message(m, SimpleNamespace(**_base(media_types=("photo",)))).accepted
    assert check_message(m, SimpleNamespace(**_base(media_types=ALL_MEDIA_TYPES))).accepted


def _base(**kw):
    d = dict(copy_text=False, media_types=ALL_MEDIA_TYPES, start_from_id=0, end_at_id=None,
             date_from=None, date_to=None, max_file_size_mb=2000)
    d.update(kw)
    return d


def test_ignore_history_env_and_override(tmp_path):
    s = load_settings(env={"IGNORE_HISTORY": "true", "DATA_PATH": str(tmp_path)}, env_file=None)
    assert s.ignore_history is True
    s.apply_overrides({"ignore_history": "false"})
    assert s.ignore_history is False


def test_old_skip_history_override_is_ignored(tmp_path):
    (tmp_path / "settings.json").write_text('{"skip_history": true}')
    s = load_settings(env={"DATA_PATH": str(tmp_path)}, env_file=None)
    assert s.ignore_history is False


async def _run(tmp_path, msgs, **kw):
    client = FakeClient(msgs)
    store = Store(tmp_path / "db.sqlite")
    fw = Forwarder(client, settings(tmp_path, media_types=ALL_MEDIA_TYPES, **kw), store, ProgressState())
    await fw.prepare()
    summary = await fw.run_history()
    return client, store, fw, summary


def test_ignore_history_transfers_already_transferred_again(tmp_path):
    import asyncio
    msgs = [make_msg(i, "photo") for i in (1, 2, 3)]
    client, store, fw, summary = asyncio.run(_run(tmp_path, msgs))
    assert summary["ok"] == 3 and len(client.sent) == 3
    # normal second run: nothing new
    summary = asyncio.run(fw.run_history())
    assert summary["ok"] == 0 and len(client.sent) == 3
    # ignore history: everything is sent again
    fw.settings.ignore_history = True
    summary = asyncio.run(fw.run_history())
    assert summary["ok"] == 3 and len(client.sent) == 6
    assert fw._live_accepts(msgs[0])
    # switched off again: history is respected
    fw.settings.ignore_history = False
    summary = asyncio.run(fw.run_history())
    assert summary["ok"] == 0 and len(client.sent) == 6
    assert not fw._live_accepts(msgs[0])


def test_document_is_transferred(tmp_path):
    import asyncio
    client, store, fw, summary = asyncio.run(_run(tmp_path, [_doc(1, "application/zip")]))
    assert summary["ok"] == 1
