"""按平台选择后端。测试可用 SCHEDULER_BACKEND=noop 或直接传实例。"""

from __future__ import annotations

import os

from app.scheduler.backends.base import Backend
from app.scheduler.backends.linux import LinuxBackend
from app.scheduler.backends.noop import NoopBackend
from app.scheduler.backends.windows import WindowsBackend

__all__ = ["Backend", "WindowsBackend", "LinuxBackend", "NoopBackend", "get_backend"]


def get_backend() -> Backend:
    forced = os.getenv("SCHEDULER_BACKEND", "").strip().lower()
    if forced == "noop":
        return NoopBackend()
    if forced == "windows":
        return WindowsBackend()
    if forced == "linux":
        return LinuxBackend()
    if os.name == "nt":
        return WindowsBackend()
    return LinuxBackend()
