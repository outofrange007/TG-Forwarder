"""Metadata and size limits for re-uploading (download fallback).

Background: the Telegram protocol encodes many fields as 32-bit integers
(``struct.pack('<i', ...)``), e.g. width/height in ``DocumentAttributeVideo``.
If metadata detection (hachoir) returns absurd values for a broken or unusual
file (e.g. ``PixelWidth`` from a Matroska container > 2^31), Telethon only fails
while serializing, with the cryptic error
``'i' format requires -2147483648 <= number <= 2147483647``. This module
therefore checks and corrects the attributes *before* sending.
"""

from __future__ import annotations

import copy
import logging
import math
import struct
from typing import Iterable, List, Optional

from telethon.tl import types
from telethon.tl.tlobject import TLObject

log = logging.getLogger("forwarder.upload")

INT32_MAX = 2**31 - 1
MB = 1024 * 1024
# Telegram: max. 4000 parts of 512 KiB = 2000 MiB (Premium: 8000 parts = 4000 MiB)
UPLOAD_LIMIT_BYTES = 2000 * MB
PREMIUM_UPLOAD_LIMIT_BYTES = 4000 * MB

_SERIALIZE_ERRORS = (struct.error, OverflowError, TypeError, ValueError)


class FileTooLargeError(RuntimeError):
    """File exceeds Telegram's upload limit (retrying makes no sense)."""


def upload_limit(premium: bool) -> int:
    return PREMIUM_UPLOAD_LIMIT_BYTES if premium else UPLOAD_LIMIT_BYTES


def check_upload_size(size: Optional[int], premium: bool, name: str) -> None:
    """Raises ``FileTooLargeError`` with an understandable message."""
    if not size or size <= upload_limit(premium):
        return
    limit_mb = upload_limit(premium) // MB
    hint = ("not possible even with Telegram Premium" if premium
            else "with Telegram Premium up to 4000 MB would be possible")
    raise FileTooLargeError(
        f"'{name}' is {size / MB:.0f} MB - Telegram allows at most "
        f"{limit_mb} MB per upload ({hint}). The source has content protection, so the only "
        f"option is download and re-upload. The file is skipped.")


def serializable(obj) -> bool:
    try:
        bytes(obj)
        return True
    except _SERIALIZE_ERRORS:
        return False


def find_invalid_values(obj, path: str = "") -> List[str]:
    """Diagnostics: deepest TL objects that cannot be serialized, with integer fields.

    Beispiel: ``['DocumentAttributeVideo.w=4294967295']``.
    """
    if isinstance(obj, list):
        return [p for i, o in enumerate(obj) for p in find_invalid_values(o, f"{path}[{i}]")]
    if not isinstance(obj, TLObject) or serializable(obj):
        return []
    name = f"{path}.{type(obj).__name__}" if path else type(obj).__name__
    found = [p for k, v in vars(obj).items() for p in find_invalid_values(v, f"{name}.{k}")]
    if found:
        return found
    bad = [f"{name}.{k}={v}" for k, v in vars(obj).items()
           if isinstance(v, int) and not isinstance(v, bool) and not -2**31 <= v <= INT32_MAX]
    return bad or [name]


def _dim(value) -> int:
    """Width/height: valid 1 ... 2^31-1, otherwise 1 (like Telethon's default)."""
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        return 1
    return v if 1 <= v <= INT32_MAX else 1


def _duration(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return v if math.isfinite(v) and 0 <= v <= INT32_MAX else 0.0


def sanitize_attributes(attributes: Iterable) -> List:
    """Clamps duration/width/height to valid ranges and discards non-serializable attributes.

    The passed objects are not modified (copies are used) because they may
    belong to the original message.
    """
    result = []
    for attr in attributes or ():
        a = copy.copy(attr)
        if isinstance(a, types.DocumentAttributeVideo):
            a.w, a.h = _dim(a.w), _dim(a.h)
            a.duration = _duration(a.duration)
            pps = getattr(a, "preload_prefix_size", None)
            if pps is not None and not (isinstance(pps, int) and 0 <= pps <= INT32_MAX):
                a.preload_prefix_size = None
            ts = getattr(a, "video_start_ts", None)
            if ts is not None and not (isinstance(ts, (int, float)) and math.isfinite(ts) and ts >= 0):
                a.video_start_ts = None
        elif isinstance(a, types.DocumentAttributeImageSize):
            a.w, a.h = _dim(a.w), _dim(a.h)
        elif isinstance(a, types.DocumentAttributeAudio):
            a.duration = int(_duration(a.duration))
        if serializable(a):
            result.append(a)
        else:
            log.warning("Discarding invalid attribute: %s", ", ".join(find_invalid_values(a)))
    return result


def minimal_attributes(name: str) -> List:
    """Emergency attributes: only the file name (Telegram detects the rest itself or shows a file)."""
    return [types.DocumentAttributeFilename(name)]


__all__ = ["FileTooLargeError", "INT32_MAX", "UPLOAD_LIMIT_BYTES", "PREMIUM_UPLOAD_LIMIT_BYTES",
           "upload_limit", "check_upload_size", "serializable", "find_invalid_values",
           "sanitize_attributes", "minimal_attributes"]
