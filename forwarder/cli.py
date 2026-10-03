"""Command line: ``python -m forwarder <command>``.

Commands:
  web      Start the web dashboard (default)
  login    Create the Telegram session interactively (phone, code, optional 2FA)
  run      Run the transfer without the dashboard (--live for continuous operation)
  chats    List groups/channels with their IDs
  topics   List the topics of the source group (for SOURCE_TOPIC_ID / --source-topic)
  inspect  Show messages of the source (text, media type, album) - for troubleshooting
  status   Show statistics from the database
  reset    Delete the processing history (everything will be transferred again)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from .config import ConfigError, load_settings, parse_chat_ref, parse_topic_id
from .state import ProgressState
from .store import Store


def setup_logging(settings) -> None:
    root = logging.getLogger()
    if getattr(root, "_forwarder_configured", False):
        return
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    log_dir = settings.data_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "forwarder.log", maxBytes=5 * 1024 * 1024,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    root._forwarder_configured = True


async def _connected_client(settings, interactive: bool = False):
    from .worker import make_client
    client = make_client(settings)
    if interactive:
        await client.start()  # asks for phone number, code and, if needed, the 2FA password
    else:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SystemExit("Not logged in. Please run 'python -m forwarder login' first "
                             "or log in via the web dashboard.")
    return client


async def cmd_login(settings, args) -> None:
    client = await _connected_client(settings, interactive=True)
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username or '-'}, ID {me.id}).")
    if args.print_session_string:
        from telethon.sessions import StringSession
        print("\nSESSION_STRING (keep secret!):\n" + StringSession.save(client.session))
    else:
        print(f"Session saved to {settings.session_path}.session")
    await client.disconnect()


async def cmd_chats(settings, args) -> None:
    from .engine import list_dialogs
    client = await _connected_client(settings)
    try:
        rows = await list_dialogs(client)
    finally:
        await client.disconnect()
    print(f"{'ID':>16}  {'Type':<6} {'Forum':<5} {'Prot.':<6} Title")
    for r in rows:
        print(f"{r['id']:>16}  {r['type']:<6} {'yes' if r['forum'] else '-':<5} "
              f"{'yes' if r['protected'] else '-':<6} {r['title']}"
              + (f"  (@{r['username']})" if r["username"] else ""))


async def cmd_topics(settings, args) -> None:
    from .engine import list_source_topics
    chat = parse_chat_ref(args.chat) if args.chat else settings.source_chat
    if chat is None:
        raise ConfigError("No source: set SOURCE_CHAT or pass --chat")
    client = await _connected_client(settings)
    try:
        info = await list_source_topics(client, chat)
    finally:
        await client.disconnect()
    print(f"Source: {info['title']} ({info['chat_id']})")
    if not info["forum"]:
        print("This group is not a forum - there are no topics.")
        return
    print(f"{'Topic ID':>9}  Title")
    for t in info["topics"]:
        mark = "  <- selected" if t["id"] == settings.source_topic_id else ""
        print(f"{t['id']:>9}  {t['title']}{mark}")


def _parse_ids(specs) -> list:
    ids = []
    for spec in specs:
        for part in str(spec).split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                ids.extend(range(int(a), int(b) + 1))
            elif part:
                ids.append(int(part))
    return ids


async def cmd_inspect(settings, args) -> None:
    from .media import classify, file_name
    chat = parse_chat_ref(args.chat) if args.chat else settings.source_chat
    if chat is None:
        raise ConfigError("No source: set SOURCE_CHAT or pass --chat")
    ids = _parse_ids(args.ids)
    if not ids:
        raise ConfigError("Please give message IDs, e.g. 'inspect 37' or 'inspect 37-45'")
    client = await _connected_client(settings)
    try:
        msgs = await client.get_messages(chat, ids=ids)
    finally:
        await client.disconnect()
    for mid, m in zip(ids, msgs):
        if m is None:
            print(f"#{mid}: not found")
            continue
        text = getattr(m, "message", "") or ""
        replies = getattr(m, "replies", None)
        print(f"#{m.id}: Type={classify(m) or 'text'} | File={file_name(m)} | "
              f"Album={getattr(m, 'grouped_id', None) or '-'} | Text={len(text)} chars | "
              f"Formatting={len(getattr(m, 'entities', None) or [])} | "
              f"Comments={getattr(replies, 'replies', 0) if replies else '-'}")
        if text:
            print("    " + text[:300].replace("\n", "\n    "))


async def cmd_run(settings, args) -> int:
    from .engine import Forwarder
    if args.dry_run:
        settings.dry_run = True
    if args.source_topic is not None:
        settings.source_topic_id = parse_topic_id(args.source_topic, "--source-topic")
    settings.validate()
    store = Store(settings.db_path)
    state = ProgressState()
    stop = threading.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: (print("\nStopping after the current file ..."), stop.set()))
        except (NotImplementedError, RuntimeError):  # Windows
            pass

    async def reporter():
        last = None
        while True:
            await asyncio.sleep(args.report_interval)
            s = state.snapshot()
            line = (f"[{s['elapsed']}] {s['status_text']} | {s['done']}/{s['total']} "
                    f"({s['overall_progress']}%) ok={s['ok']} failed={s['failed']} | "
                    f"{s['current_item']} {s['phase']} {s['file_progress']}% {s['speed_mbps']} MB/s"
                    + (f" | ETA {s['eta']}" if s["eta"] else ""))
            if line != last:
                print(line, flush=True)
                last = line

    client = await _connected_client(settings)
    task = asyncio.create_task(reporter())
    try:
        summary = await Forwarder(client, settings, store, state, stop).run(live=args.live)
    finally:
        task.cancel()
        await client.disconnect()
        store.close()
    print(f"Done: {summary['ok']} transferred, {summary['failed']} failed, "
          f"{summary['total']} total.")
    return 1 if summary["failed"] else 0


def cmd_status(settings, args) -> None:
    store = Store(settings.db_path)
    print("Total:", store.counts())
    for item in store.topics():
        print(f"Topic {item['title']!r}: {item['source_topic']} -> {item['target_topic']}")
    for item in store.recent(args.limit):
        print(f"{item['updated_at']} {item['source_chat']}#{item['message_id']} {item['status']} {item['info']}")
    store.close()


def cmd_reset(settings, args) -> None:
    if not args.yes:
        answer = input("Really delete the entire history? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "j", "ja"):
            print("Aborted.")
            return
    store = Store(settings.db_path)
    store.reset(include_topics=args.topics)
    store.close()
    print("History deleted.")


def cmd_web(settings, args) -> None:
    from .web import create_app
    from .worker import TelegramWorker
    store = Store(settings.db_path)
    state = ProgressState()
    worker = TelegramWorker(settings, store, state).start()
    app = create_app(settings, store, state, worker)
    port = args.port or settings.web_port
    logging.getLogger("forwarder").info("Web dashboard: http://%s:%s", settings.web_host, port)
    try:
        app.run(host=settings.web_host, port=port, debug=False, use_reloader=False, threaded=True)
    finally:
        worker.shutdown()
        store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forwarder", description="Telegram photo/video forwarder (Telethon)")
    p.add_argument("--env-file", default=".env", help="Path to the .env file (default: .env)")
    sub = p.add_subparsers(dest="command")

    w = sub.add_parser("web", help="Start the web dashboard")
    w.add_argument("--port", type=int)

    lg = sub.add_parser("login", help="Create the Telegram session")
    lg.add_argument("--print-session-string", action="store_true",
                    help="additionally print a SESSION_STRING")

    r = sub.add_parser("run", help="Transfer without the dashboard")
    r.add_argument("--live", action="store_true", help="after the history, wait for new messages")
    r.add_argument("--dry-run", action="store_true", help="only show, send nothing")
    r.add_argument("--report-interval", type=float, default=5.0)
    r.add_argument("--source-topic", metavar="ID",
                   help="only process this source topic ('all' = all; overrides SOURCE_TOPIC_ID)")

    tp = sub.add_parser("topics", help="List the topics of the source group")
    tp.add_argument("--chat", help="query a different group than SOURCE_CHAT")

    ins = sub.add_parser("inspect", help="Show messages of the source (troubleshooting)")
    ins.add_argument("ids", nargs="+", help="IDs, e.g. 37 or 37-45 or 37,39")
    ins.add_argument("--chat", help="query a different group than SOURCE_CHAT")

    sub.add_parser("chats", help="List groups/channels with IDs")
    st = sub.add_parser("status", help="Show statistics")
    st.add_argument("--limit", type=int, default=10)
    rs = sub.add_parser("reset", help="Delete history")
    rs.add_argument("--topics", action="store_true", help="also delete topic mappings")
    rs.add_argument("-y", "--yes", action="store_true", help="without confirmation")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "web"
    if command == "web" and not hasattr(args, "port"):
        args.port = None
    try:
        settings = load_settings(env_file=args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings)
    try:
        if command == "web":
            cmd_web(settings, args)
        elif command == "login":
            asyncio.run(cmd_login(settings, args))
        elif command == "run":
            return asyncio.run(cmd_run(settings, args))
        elif command == "topics":
            asyncio.run(cmd_topics(settings, args))
        elif command == "inspect":
            asyncio.run(cmd_inspect(settings, args))
        elif command == "chats":
            asyncio.run(cmd_chats(settings, args))
        elif command == "status":
            cmd_status(settings, args)
        elif command == "reset":
            cmd_reset(settings, args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    return 0
