"""Thread-safe progress state (for dashboard and console)."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable, Optional

MB = 1024 * 1024


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


class ProgressState:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.started_at = clock()
        self.events = deque(maxlen=50)
        self.reset_job()

    def reset_job(self, mode: str = "idle") -> None:
        with getattr(self, "_lock"):
            self.running = mode != "idle"
            self.job = mode            # idle | history | live
            self.status = "Ready"
            self.current_item = "-"
            self.phase = ""            # senden | download | upload
            self.total = 0
            self.done = 0
            self.ok = 0
            self.skipped = 0
            self.failed = 0
            self.file_progress = 0.0
            self.file_size_mb = 0.0
            self.speed_mbps = 0.0
            self.job_started_at = self._clock() if self.running else None

    def update(self, **kwargs) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if not hasattr(self, key):
                    raise AttributeError(key)
                setattr(self, key, value)

    def log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self.events.appendleft({
                "time": time.strftime("%H:%M:%S", time.localtime(self._clock())),
                "level": level,
                "text": text,
            })

    def item_finished(self, result: str, count: int = 1) -> None:
        """result: ok | skipped | failed"""
        with self._lock:
            self.done += count
            setattr(self, result, getattr(self, result) + count)
            self.file_progress = 100.0 if result == "ok" else self.file_progress
            self.speed_mbps = 0.0

    @property
    def overall_progress(self) -> float:
        if not self.total:
            return 0.0
        return min(100.0, self.done / self.total * 100)

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            elapsed = now - self.job_started_at if self.job_started_at else 0
            eta = None
            if self.running and self.job == "history" and self.done and self.total > self.done:
                eta = format_duration(elapsed / self.done * (self.total - self.done))
            return {
                "running": self.running,
                "job": self.job,
                "status_text": self.status,
                "current_item": self.current_item,
                "phase": self.phase,
                "total": self.total,
                "done": self.done,
                "ok": self.ok,
                "skipped": self.skipped,
                "failed": self.failed,
                "overall_progress": round(self.overall_progress, 1),
                "file_progress": round(self.file_progress, 1),
                "file_size_mb": round(self.file_size_mb, 2),
                "speed_mbps": round(self.speed_mbps, 2),
                "elapsed": format_duration(elapsed),
                "eta": eta,
                "uptime": format_duration(now - self.started_at),
                "events": list(self.events),
            }

    def progress_callback(self, phase: str, interval: float = 0.5,
                          on_tick: Optional[Callable[[str], None]] = None, count_mode: bool = False):
        """Creates a Telethon ``progress_callback(current, total)`` with speed measurement.

        ``count_mode``: for albums Telethon reports (files sent, files total)
        instead of bytes - then only a percentage, no size/speed.
        """
        last = {"bytes": 0, "time": self._clock()}

        def callback(current: int, total: int) -> None:
            now = self._clock()
            dt = now - last["time"]
            pct = (current / total * 100) if total else 0.0
            if dt >= interval or (total and current >= total):
                speed = ((current - last["bytes"]) / dt / MB) if dt > 0 else 0.0
                last["bytes"], last["time"] = current, now
                with self._lock:
                    self.phase = phase
                    self.file_progress = pct
                    if not count_mode:
                        self.file_size_mb = total / MB if total else 0.0
                        self.speed_mbps = max(0.0, speed)
                if on_tick:
                    detail = (f"{current:.1f}/{total} files" if count_mode
                              else f"{current / MB:.1f}/{total / MB:.1f} MB")
                    on_tick(f"{phase}: {pct:5.1f}% ({detail})")

        return callback
