"""Flask web dashboard: login, settings, start/stop, progress."""

from __future__ import annotations

import hmac
import logging
from functools import wraps

from flask import Flask, Response, jsonify, render_template, request

from .config import EDITABLE_FIELDS, ConfigError, parse_chat_ref

log = logging.getLogger("forwarder.web")


def _error(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def create_app(settings, store, state, worker) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False

    # Optional password protection (HTTP Basic Auth) via WEB_PASSWORD
    @app.before_request
    def _require_password():
        if not settings.web_password:
            return None
        auth = request.authorization
        if auth and hmac.compare_digest(auth.password or "", settings.web_password):
            return None
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="Telegram Forwarder"'})

    def telegram_call(fn):
        """Catches errors from Telegram calls and returns them as JSON."""
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ConfigError, RuntimeError, ValueError) as exc:
                return _error(str(exc))
            except Exception as exc:  # e.g. Telethon RPC error
                log.exception("API error")
                return _error(f"{type(exc).__name__}: {exc}", 500)
        return wrapper

    def body() -> dict:
        return request.get_json(silent=True) or {}

    @app.route("/")
    def index():
        return render_template("index.html")

    # --- login ---
    @app.route("/api/auth/status")
    @telegram_call
    def auth_status():
        data = {"configured": settings.has_credentials, "authorized": False}
        if settings.has_credentials:
            data.update(worker.call(worker.auth_status(), timeout=30))
        return jsonify(data)

    @app.route("/api/auth/send_code", methods=["POST"])
    @telegram_call
    def send_code():
        phone = str(body().get("phone", "")).strip().replace(" ", "")
        if not phone.startswith("+") or not phone[1:].isdigit():
            return _error("Enter the phone number in international format, e.g. +491701234567")
        return jsonify(worker.call(worker.send_code(phone)))

    @app.route("/api/auth/sign_in", methods=["POST"])
    @telegram_call
    def sign_in():
        code = str(body().get("code", "")).strip()
        if not code:
            return _error("Code missing")
        return jsonify(worker.call(worker.sign_in(code)))

    @app.route("/api/auth/password", methods=["POST"])
    @telegram_call
    def password():
        pw = str(body().get("password", ""))
        if not pw:
            return _error("Password missing")
        return jsonify(worker.call(worker.sign_in_password(pw)))

    @app.route("/api/auth/logout", methods=["POST"])
    @telegram_call
    def logout():
        if worker.running:
            return _error("Stop the running job first")
        return jsonify(worker.call(worker.logout()))

    # --- status & control ---
    @app.route("/api/status")
    def status():
        data = state.snapshot()
        data["running"] = bool(worker.running)
        data["totals"] = store.counts()
        return jsonify(data)

    @app.route("/api/start", methods=["POST"])
    @telegram_call
    def start():
        worker.start_job(live=bool(body().get("live", False)))
        return jsonify({"success": True})

    @app.route("/api/stop", methods=["POST"])
    def stop():
        worker.stop_job()
        return jsonify({"success": True})

    # --- settings ---
    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        return jsonify({"settings": settings.public_dict(), "editable": list(EDITABLE_FIELDS)})

    @app.route("/api/settings", methods=["POST"])
    @telegram_call
    def save_settings():
        if worker.running:
            return _error("Settings cannot be changed while a job is running")
        changes = {k: v for k, v in body().items() if k in EDITABLE_FIELDS}
        if not changes:
            return _error("No changeable fields provided")
        backup = {k: getattr(settings, k) for k in changes}
        try:
            settings.apply_overrides(changes)
            settings.validate(require_chats=False)
        except ConfigError:
            for k, v in backup.items():
                setattr(settings, k, v)
            raise
        settings.save_overrides({k: getattr(settings, k) for k in changes})
        return jsonify({"success": True, "settings": settings.public_dict()})

    @app.route("/api/chats")
    @telegram_call
    def chats():
        return jsonify({"chats": worker.call(worker.dialogs(), timeout=90)})

    @app.route("/api/source_topics")
    @telegram_call
    def source_topics():
        """Topics of the source (``?chat=`` overrides the saved source)."""
        chat = parse_chat_ref(request.args.get("chat")) if request.args.get("chat") else None
        data = worker.call(worker.source_topics(chat), timeout=60)
        data["selected"] = settings.source_topic_id
        return jsonify(data)

    @app.route("/api/history")
    def history():
        limit = min(int(request.args.get("limit", 25)), 200)
        return jsonify({"items": store.recent(limit), "topics": store.topics()})

    @app.route("/api/reset", methods=["POST"])
    def reset():
        if worker.running:
            return _error("Stop the running job first")
        store.reset(include_topics=bool(body().get("topics", False)))
        return jsonify({"success": True})

    return app
