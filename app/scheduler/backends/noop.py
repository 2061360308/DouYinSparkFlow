"""空后端：只在测试或显式关闭注册时使用，不碰任何系统状态。"""

from __future__ import annotations

from app.scheduler.backends.base import Backend


class NoopBackend(Backend):
    name = "noop"

    def __init__(self) -> None:
        self.installed: dict | None = None

    def available(self) -> tuple:
        return True, ""

    def install(self, *, name: str, mode: str, run_time: str, launcher) -> None:
        self.installed = {
            "name": name,
            "mode": mode,
            "run_time": run_time,
            "launcher": str(launcher),
        }

    def uninstall(self, *, name: str) -> bool:
        existed = self.installed is not None
        self.installed = None
        return existed

    def status(self, *, name: str, mode: str = "") -> dict:
        installed = bool(self.installed and self.installed.get("name") == name)
        return {"installed": installed, "detail": ""}
