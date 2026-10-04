"""系统任务后端：把「注册 / 卸载 / 查询」抽象成各平台实现。

- Windows：定时用 schtasks（动作是隐藏 VBS），开机用「启动」文件夹里的 VBS（免管理员）。
- Linux：用户级 crontab（`M H * * *` / `@reboot`），免 root。
- Noop：测试 / 干跑用，不碰系统。

测试可用环境变量 SCHEDULER_BACKEND=noop，或直接构造实例。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from app import paths
from app.scheduler import core
from app.util import no_window_kwargs


def _default_run(args, input_text=None):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        input=input_text,
        **no_window_kwargs(),
    )


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------
class Backend:
    """把「注册/卸载/查询」一个系统任务抽象出来。"""

    name = "base"

    def available(self) -> tuple:
        """返回 (是否可用, 不可用原因)。"""
        return True, ""

    def install(self, *, name: str, mode: str, run_time: str, launcher) -> None:
        """注册任务。mode 为 'scheduled'（每天定点）或 'boot'（开机/登录）。"""
        raise NotImplementedError

    def uninstall(self, *, name: str) -> bool:
        """卸载任务，返回是否真的删掉了。任务不存在也当成功（幂等）。"""
        raise NotImplementedError

    def status(self, *, name: str, mode: str = "") -> dict:
        """返回 {'installed': bool, 'detail': str}。mode 用于区分后端机制。"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------
class WindowsBackend(Backend):
    """定时：schtasks + 隐藏 VBS；开机：启动文件夹里的隐藏 VBS。"""

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
            # 定点执行：走 run-if-due --force —— 每次都跑，同时记录执行历史
            vbs = core.write_vbs(
                launcher, self.task_vbs(), launcher.scheduler_argv("run-if-due", "--force")
            )
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


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------
class LinuxBackend(Backend):
    """用户级 crontab：scheduled = `M H * * *`，boot = `@reboot`。

    用注释 marker 做幂等：安装时先按 marker 过滤掉旧行再追加；卸载同理。
    """

    name = "linux-crontab"

    def __init__(self, runner=None) -> None:
        self._run = runner or _default_run

    def available(self) -> tuple:
        if os.name == "nt":
            return False, "当前不是 Linux/macOS"
        if shutil.which("crontab") is None:
            return False, "找不到 crontab（未安装 cron）"
        return True, ""

    @staticmethod
    def marker(name: str) -> str:
        return f"# {name}"

    def build_line(self, *, name: str, mode: str, run_time: str, wrapper) -> str:
        cmd = shlex.quote(str(wrapper))
        marker = self.marker(name)
        if mode == "scheduled":
            hour, _, minute = run_time.partition(":")
            return f"{int(minute)} {int(hour)} * * * {cmd} {marker}"
        if mode == "boot":
            return f"@reboot {cmd} {marker}"
        raise ValueError(f"Linux 后端不支持的模式：{mode!r}")

    def _read_crontab(self) -> str:
        result = self._run(["crontab", "-l"])
        return result.stdout if result.returncode == 0 else ""

    def _write_crontab(self, text: str) -> None:
        result = self._run(["crontab", "-"], input_text=text)
        if result.returncode != 0:
            raise RuntimeError(
                f"写入 crontab 失败：{(result.stderr or result.stdout or '').strip()}"
            )

    def _strip(self, text: str, name: str) -> list:
        marker = self.marker(name)
        return [line for line in text.splitlines() if marker not in line]

    def install(self, *, name: str, mode: str, run_time: str, launcher) -> None:
        if mode == "scheduled":
            # 定点执行：走 run-if-due --force —— 每次都跑，同时记录执行历史
            wrapper = core.write_sh(
                launcher,
                paths.SCHEDULER_DIR / "run_task.sh",
                launcher.scheduler_argv("run-if-due", "--force"),
            )
        elif mode == "boot":
            wrapper = core.write_sh(
                launcher,
                paths.SCHEDULER_DIR / "run_if_due.sh",
                launcher.scheduler_argv("run-if-due"),
            )
        else:
            raise ValueError(f"Linux 后端不支持的模式：{mode!r}")

        lines = self._strip(self._read_crontab(), name)
        lines.append(self.build_line(name=name, mode=mode, run_time=run_time, wrapper=wrapper))
        self._write_crontab("\n".join(lines) + "\n")

    def uninstall(self, *, name: str) -> bool:
        original = self._read_crontab()
        lines = self._strip(original, name)
        if lines == original.splitlines():
            return False
        self._write_crontab("\n".join(lines) + ("\n" if lines else ""))
        return True

    def status(self, *, name: str, mode: str = "") -> dict:
        marker = self.marker(name)
        text = self._read_crontab()
        for line in text.splitlines():
            if marker in line:
                return {"installed": True, "detail": line.strip()}
        return {"installed": False, "detail": ""}


# ---------------------------------------------------------------------------
# Noop（测试 / 干跑）
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 选择
# ---------------------------------------------------------------------------
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
