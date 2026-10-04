"""Windows 后端。

- 定时（scheduled）：用 schtasks 注册每天定点任务，动作是 `wscript.exe <run_task.vbs>`
  —— 由 VBS 隐藏运行，执行时**没有控制台窗口**。
- 开机（boot）：把 `<name>.vbs` 放进用户「启动」文件夹，登录时由系统自动隐藏运行
  `scheduler run-if-due`。**不需要管理员权限**（schtasks 的 ONLOGON 会要求提权）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.scheduler import core, paths
from app.scheduler.backends.base import Backend


def _default_run(args):
    return subprocess.run(args, capture_output=True, text=True)


class WindowsBackend(Backend):
    name = "windows-schtasks"

    def __init__(self, runner=None) -> None:
        self._run = runner or _default_run

    def available(self) -> tuple:
        if os.name != "nt":
            return False, "当前不是 Windows"
        if shutil.which("schtasks") is None:
            return False, "找不到 schtasks.exe"
        return True, ""

    # -- 路径 -------------------------------------------------------------
    def startup_dir(self) -> Path:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("找不到 APPDATA，无法定位启动文件夹")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

    def boot_vbs(self, name: str) -> Path:
        return self.startup_dir() / f"{name}.vbs"

    def task_vbs(self) -> Path:
        return paths.SCHEDULER_DIR / "run_task.vbs"

    # -- 命令（纯函数，便于单测） ----------------------------------------
    def build_scheduled_command(self, *, name: str, run_time: str, vbs) -> list:
        return [
            "schtasks", "/Create", "/F",
            "/TN", name,
            "/TR", f'wscript.exe "{vbs}"',
            "/SC", "DAILY", "/ST", run_time,
        ]

    # -- 安装 / 卸载 / 状态 ----------------------------------------------
    def install(self, *, name: str, mode: str, run_time: str, launcher) -> None:
        if mode == "scheduled":
            vbs = core.write_vbs(launcher, self.task_vbs(), launcher.task_argv())
            result = self._run(
                self.build_scheduled_command(name=name, run_time=run_time, vbs=vbs)
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"注册计划任务失败（{name}）："
                    f"{(result.stderr or result.stdout or '').strip()}"
                )
        elif mode == "boot":
            core.write_vbs(launcher, self.boot_vbs(name), launcher.scheduler_argv("run-if-due"))
        else:
            raise ValueError(f"Windows 后端不支持的模式：{mode!r}")

    def uninstall(self, *, name: str) -> bool:
        result = self._run(["schtasks", "/Delete", "/TN", name, "/F"])
        targets = [self.task_vbs()]
        try:
            targets.append(self.boot_vbs(name))
        except Exception:
            pass
        for path in targets:
            try:
                path.unlink()
            except OSError:
                pass
        return result.returncode == 0

    def status(self, *, name: str, mode: str = "") -> dict:
        if mode == "boot":
            try:
                path = self.boot_vbs(name)
            except Exception:
                return {"installed": False, "detail": ""}
            return {"installed": path.is_file(), "detail": str(path)}
        result = self._run(["schtasks", "/Query", "/TN", name])
        detail = (result.stdout or result.stderr or "").strip()
        return {"installed": result.returncode == 0, "detail": detail}
