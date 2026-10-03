"""Configuration: reads settings from environment variables or a .env file.

In addition, some non-critical fields (source, target, topic, mode ...) can be
overridden via the web dashboard; these are stored in ``DATA_PATH/settings.json``.
Credentials (API_ID/API_HASH/SESSION_STRING) come exclusively from the environment.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional (e.g. in Docker via env_file)
    load_dotenv = None

ChatRef = Union[int, str]

ALL_MEDIA_TYPES = ("photo", "video", "animation", "video_note", "image_file", "video_file")
DEFAULT_MEDIA_TYPES = ("photo", "video", "image_file", "video_file")
FORWARD_MODES = ("copy", "forward")
TOPIC_MODES = ("none", "fixed", "mirror")

# Fields that may be changed via the dashboard (no secrets!)
EDITABLE_FIELDS = (
    "source_chat", "source_topic_id", "target_chat", "target_topic_id",
    "topic_mode", "mode", "media_types", "caption_template", "hide_sender",
    "delay_seconds", "start_from_id", "end_at_id", "max_file_size_mb",
    "download_fallback", "copy_text", "dry_run",
)

_TG_LINK = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(.+)$", re.I)


class ConfigError(ValueError):
    """Invalid or missing configuration."""


def parse_bool(value, default: bool = False) -> bool:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in ("1", "true", "yes", "ja", "on", "y", "j"):
        return True
    if v in ("0", "false", "no", "nein", "off", "n"):
        return False
    raise ConfigError(f"Not a valid boolean value: {value!r}")


def parse_int(value, name: str, default: Optional[int] = None) -> Optional[int]:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return default
    try:
        return int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, not {value!r}") from exc


def parse_chat_ref(value) -> Optional[ChatRef]:
    """Normalizes a chat reference.

    Allowed: numeric IDs (``-1001234567890``), ``@username``, ``username``,
    ``https://t.me/username`` and invite links (``https://t.me/+abc`` or
    ``t.me/joinchat/abc``), which are left unchanged.
    Links to private channels (``t.me/c/1234567890/55``) become ``-1001234567890``.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    v = str(value).strip()
    if not v:
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    m = _TG_LINK.match(v)
    if m:
        path = m.group(1).strip("/")
        if path.startswith("+") or path.lower().startswith("joinchat/"):
            return v if v.lower().startswith("http") else "https://" + v
        if path.lower().startswith("c/"):
            parts = path.split("/")
            if len(parts) >= 2 and parts[1].isdigit():
                return int("-100" + parts[1])
            raise ConfigError(f"Invalid t.me/c link: {value!r}")
        username = path.split("/")[0]
        return "@" + username.lstrip("@")
    if re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,}", v):
        return "@" + v.lstrip("@")
    raise ConfigError(f"Unknown chat format: {value!r}")


MAX_INT32 = 2**31 - 1


def parse_topic_ref(value, name: str) -> Optional[int]:
    """Topic ID as a number or taken from a Telegram link.

    ``t.me/c/1234567890/55`` or ``t.me/c/1234567890/55/77`` -> 55,
    ``t.me/group/55`` -> 55. Topic IDs are message IDs (32-bit, positive);
    chat IDs such as ``-100...`` are rejected with a clear message.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    m = _TG_LINK.match(v)
    if m:
        parts = [p for p in m.group(1).strip("/").split("/") if p]
        if parts and parts[0].lower() == "c":
            parts = parts[1:]
        if len(parts) >= 2 and parts[1].isdigit():
            v = parts[1]
        else:
            raise ConfigError(f"{name}: link contains no topic ID: {value!r}")
    topic = parse_int(v, name)
    if topic is None or topic == 0:
        return None
    if topic < 0 or topic > MAX_INT32:
        raise ConfigError(
            f"{name}={topic} is not a valid topic ID (allowed: 1 to {MAX_INT32}). "
            "You probably entered a chat ID - the topic ID is the small number "
            "in the topic link, e.g. 55 in t.me/c/1234567890/55")
    return topic


def parse_topic_id(value, name: str = "SOURCE_TOPIC_ID") -> Optional[int]:
    """Topic ID; empty, ``0``, ``all``/``alle`` mean: all topics."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("", "0", "all", "alle", "*"):
        return None
    return parse_topic_ref(value, name)


def parse_media_types(value) -> tuple:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_MEDIA_TYPES
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    result = []
    for raw in items:
        t = str(raw).strip().lower()
        if not t:
            continue
        if t == "all":
            return ALL_MEDIA_TYPES
        if t not in ALL_MEDIA_TYPES:
            raise ConfigError(
                f"Unknown media type {t!r}. Allowed: {', '.join(ALL_MEDIA_TYPES)}, all"
            )
        if t not in result:
            result.append(t)
    if not result:
        raise ConfigError("MEDIA_TYPES must not be empty")
    return tuple(result)


def parse_date(value, name: str) -> Optional[datetime]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be in the format YYYY-MM-DD[THH:MM]") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Settings:
    # Credentials
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    session_string: Optional[str] = None
    # Source / target
    source_chat: Optional[ChatRef] = None
    source_topic_id: Optional[int] = None
    target_chat: Optional[ChatRef] = None
    target_topic_id: Optional[int] = None
    topic_mode: str = "none"
    # Behavior
    mode: str = "copy"
    media_types: tuple = DEFAULT_MEDIA_TYPES
    caption_template: str = "{caption}"
    hide_sender: bool = True
    delay_seconds: float = 2.0
    start_from_id: int = 0
    end_at_id: Optional[int] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    max_file_size_mb: float = 2000.0
    download_fallback: bool = True
    copy_text: bool = False
    resume: bool = True
    dry_run: bool = False
    max_retries: int = 3
    # Infrastructure
    data_path: Path = field(default_factory=lambda: Path("./data"))
    web_host: str = "0.0.0.0"
    web_port: int = 5000
    web_password: Optional[str] = None
    log_level: str = "INFO"

    # --- derived paths ---
    @property
    def session_path(self) -> str:
        return str(self.data_path / "forwarder")

    @property
    def db_path(self) -> Path:
        return self.data_path / "forwarder.db"

    @property
    def overrides_path(self) -> Path:
        return self.data_path / "settings.json"

    @property
    def tmp_path(self) -> Path:
        return self.data_path / "tmp"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def validate(self, require_chats: bool = True) -> None:
        errors = []
        if not self.has_credentials:
            errors.append("API_ID and API_HASH are missing (see https://my.telegram.org)")
        elif not 0 < self.api_id <= 2**31 - 1:
            # Telethon packs API_ID as a 32-bit value -> otherwise a cryptic struct error on connect
            errors.append("API_ID is invalid (must be a positive number up to 2147483647, "
                          "see https://my.telegram.org)")
        if require_chats:
            if self.source_chat is None:
                errors.append("SOURCE_CHAT is missing")
            if self.target_chat is None:
                errors.append("TARGET_CHAT is missing")
        if self.mode not in FORWARD_MODES:
            errors.append(f"MODE must be one of {FORWARD_MODES}")
        if self.topic_mode not in TOPIC_MODES:
            errors.append(f"TOPIC_MODE must be one of {TOPIC_MODES}")
        if self.topic_mode == "fixed" and not self.target_topic_id:
            errors.append("TOPIC_MODE=fixed requires TARGET_TOPIC_ID")
        if self.delay_seconds < 0:
            errors.append("DELAY_SECONDS must not be negative")
        if self.end_at_id is not None and self.end_at_id < self.start_from_id:
            errors.append("END_AT_ID must be >= START_FROM_ID")
        if errors:
            raise ConfigError("; ".join(errors))

    def public_dict(self) -> dict:
        """Representation for the dashboard - without secrets."""
        data = {}
        for f in fields(self):
            if f.name in ("api_hash", "session_string", "web_password"):
                continue
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            elif isinstance(value, tuple):
                value = list(value)
            data[f.name] = value
        data["api_hash_set"] = bool(self.api_hash)
        data["session_string_set"] = bool(self.session_string)
        return data

    # --- overrides from the dashboard ---
    def apply_overrides(self, overrides: dict) -> None:
        for key, value in overrides.items():
            if key not in EDITABLE_FIELDS:
                raise ConfigError(f"Field {key!r} cannot be changed")
            setattr(self, key, _coerce(key, value))

    def save_overrides(self, overrides: dict) -> None:
        current = load_overrides(self.overrides_path)
        current.update({k: _serialize(v) for k, v in overrides.items()})
        self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.overrides_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.overrides_path)


def _serialize(value):
    if isinstance(value, tuple):
        return list(value)
    return value


def _coerce(key: str, value):
    """Converts raw values (from env/JSON/form) to the proper type."""
    if key in ("source_chat", "target_chat"):
        return parse_chat_ref(value)
    if key == "source_topic_id":
        return parse_topic_id(value)
    if key == "target_topic_id":
        return parse_topic_ref(value, "TARGET_TOPIC_ID")
    if key == "end_at_id":
        return parse_int(value, key.upper())
    if key == "start_from_id":
        return parse_int(value, key.upper(), 0) or 0
    if key in ("hide_sender", "download_fallback", "copy_text", "dry_run", "resume"):
        return parse_bool(value)
    if key in ("delay_seconds", "max_file_size_mb"):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{key.upper()} must be a number") from exc
    if key == "media_types":
        return parse_media_types(value)
    if key in ("mode", "topic_mode"):
        return str(value).strip().lower()
    if key == "caption_template":
        return "" if value is None else str(value).replace("\\n", "\n")
    return value


def load_overrides(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_settings(env: Optional[dict] = None, env_file: Optional[str] = ".env",
                  apply_saved_overrides: bool = True) -> Settings:
    """Creates Settings from ``env`` (default: os.environ, incl. .env file)."""
    if env is None:
        if load_dotenv and env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)
        env = dict(os.environ)

    def g(name, default=None):
        value = env.get(name)
        return default if value is None or str(value).strip() == "" else value

    s = Settings(
        api_id=parse_int(g("API_ID"), "API_ID"),
        api_hash=g("API_HASH"),
        session_string=g("SESSION_STRING"),
        source_chat=parse_chat_ref(g("SOURCE_CHAT")),
        source_topic_id=parse_topic_id(g("SOURCE_TOPIC_ID")),
        target_chat=parse_chat_ref(g("TARGET_CHAT")),
        target_topic_id=parse_topic_ref(g("TARGET_TOPIC_ID"), "TARGET_TOPIC_ID"),
        topic_mode=str(g("TOPIC_MODE", "")).lower() or None,
        mode=str(g("MODE", "copy")).lower(),
        media_types=parse_media_types(g("MEDIA_TYPES")),
        caption_template=_coerce("caption_template", env.get("CAPTION_TEMPLATE", "{caption}")),
        hide_sender=parse_bool(g("HIDE_SENDER"), True),
        delay_seconds=float(g("DELAY_SECONDS", 2.0)),
        start_from_id=parse_int(g("START_FROM_ID"), "START_FROM_ID", 0),
        end_at_id=parse_int(g("END_AT_ID"), "END_AT_ID"),
        date_from=parse_date(g("DATE_FROM"), "DATE_FROM"),
        date_to=parse_date(g("DATE_TO"), "DATE_TO"),
        max_file_size_mb=float(g("MAX_FILE_SIZE_MB", 2000)),
        download_fallback=parse_bool(g("DOWNLOAD_FALLBACK"), True),
        copy_text=parse_bool(g("COPY_TEXT"), False),
        resume=parse_bool(g("RESUME"), True),
        dry_run=parse_bool(g("DRY_RUN"), False),
        max_retries=parse_int(g("MAX_RETRIES"), "MAX_RETRIES", 3),
        data_path=Path(g("DATA_PATH", "./data")),
        web_host=g("WEB_HOST", "0.0.0.0"),
        web_port=parse_int(g("WEB_PORT"), "WEB_PORT", 5000),
        web_password=g("WEB_PASSWORD"),
        log_level=str(g("LOG_LEVEL", "INFO")).upper(),
    )
    # derive TOPIC_MODE automatically if not set
    if not s.topic_mode:
        s.topic_mode = "fixed" if s.target_topic_id else "none"

    if apply_saved_overrides:
        overrides = load_overrides(s.overrides_path)
        if overrides:
            s.apply_overrides({k: v for k, v in overrides.items() if k in EDITABLE_FIELDS})
    return s


__all__ = [
    "Settings", "ConfigError", "load_settings", "parse_chat_ref", "parse_bool", "parse_topic_id",
    "parse_media_types", "EDITABLE_FIELDS", "ALL_MEDIA_TYPES",
]
