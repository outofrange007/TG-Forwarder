"""Telegram background thread for the web dashboard.

Flask runs synchronously in several threads, Telethon needs a single asyncio
loop. The worker owns the loop and the client; the dashboard calls coroutines
thread-safely via ``call()``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import Optional

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from .engine import Forwarder, list_dialogs, list_source_topics
from .state import ProgressState
from .store import Store

log = logging.getLogger("forwarder.worker")


def make_client(settings) -> TelegramClient:
    """Session file under DATA_PATH or (if set) SESSION_STRING."""
    session = StringSession(settings.session_string) if settings.session_string else settings.session_path
    settings.data_path.mkdir(parents=True, exist_ok=True)
    return TelegramClient(session, settings.api_id, settings.api_hash,
                          device_model="Telegram Forwarder", app_version="1.0")


class TelegramWorker:
    def __init__(self, settings, store: Store, state: ProgressState):
        self.settings = settings
        self.store = store
        self.state = state
        self.client: Optional[TelegramClient] = None
        self.stop_event = threading.Event()
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="telegram", daemon=True)
        self._job: Optional[Future] = None
        self._phone: Optional[str] = None
        self._phone_code_hash: Optional[str] = None
        self._lock = threading.Lock()

    # --- loop ---
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> "TelegramWorker":
        if not self._thread.is_alive():
            self._thread.start()
        return self

    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro, timeout: float = 60):
        return self.submit(coro).result(timeout)

    async def _ensure_client(self) -> TelegramClient:
        if not self.settings.has_credentials:
            raise RuntimeError("API_ID/API_HASH missing in the .env")
        if self.client is None:
            self.client = make_client(self.settings)
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    # --- login ---
    async def auth_status(self) -> dict:
        client = await self._ensure_client()
        if not await client.is_user_authorized():
            return {"authorized": False}
        me = await client.get_me()
        return {"authorized": True, "user": {
            "id": me.id, "name": " ".join(filter(None, [me.first_name, me.last_name])),
            "username": me.username, "bot": bool(me.bot)}}

    async def send_code(self, phone: str) -> dict:
        client = await self._ensure_client()
        sent = await client.send_code_request(phone)
        self._phone, self._phone_code_hash = phone, sent.phone_code_hash
        return {"success": True}

    async def sign_in(self, code: str) -> dict:
        client = await self._ensure_client()
        if not self._phone:
            return {"success": False, "error": "Please request the code first"}
        try:
            await client.sign_in(self._phone, code.strip(), phone_code_hash=self._phone_code_hash)
        except errors.SessionPasswordNeededError:
            return {"success": False, "password_required": True}
        except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as exc:
            return {"success": False, "error": f"Code invalid or expired ({type(exc).__name__})"}
        return {"success": True}

    async def sign_in_password(self, password: str) -> dict:
        client = await self._ensure_client()
        try:
            await client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            return {"success": False, "error": "Wrong 2FA password"}
        return {"success": True}

    async def logout(self) -> dict:
        client = await self._ensure_client()
        await client.log_out()
        self.client = None
        return {"success": True}

    async def dialogs(self) -> list:
        return await list_dialogs(await self._ensure_client())

    async def source_topics(self, chat=None) -> dict:
        chat = chat if chat is not None else self.settings.source_chat
        if chat is None:
            raise ValueError("No source specified")
        return await list_source_topics(await self._ensure_client(), chat)

    # --- Jobs ---
    @property
    def running(self) -> bool:
        return self._job is not None and not self._job.done()

    def start_job(self, live: bool = False) -> None:
        with self._lock:
            if self.running:
                raise RuntimeError("A job is already running")
            self.settings.validate()
            self.stop_event.clear()
            self.state.reset_job("history")
            self.state.update(status="Starting ...")
            self._job = self.submit(self._run_job(live))

    async def _run_job(self, live: bool) -> None:
        try:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("Not logged in")
            forwarder = Forwarder(client, self.settings, self.store, self.state, self.stop_event)
            await forwarder.run(live=live)
            self.state.update(status="Stopped" if self.stop_event.is_set() else "Done")
        except Exception as exc:
            log.exception("Job aborted")
            self.state.update(status=f"Error: {exc}")
            self.state.log(f"Aborted: {exc}", "error")
        finally:
            self.state.update(running=False, current_item="-", phase="",
                              speed_mbps=0.0, job_started_at=None)

    def stop_job(self) -> None:
        if self.running:
            self.stop_event.set()
            self.state.update(status="Stopping after the current file ...")

    def shutdown(self) -> None:
        self.stop_event.set()
        if self.client is not None:
            try:
                self.call(self.client.disconnect(), timeout=10)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)
