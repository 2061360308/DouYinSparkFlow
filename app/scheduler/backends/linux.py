"""Linux 后端：用用户级 crontab 注册任务（零依赖，免 root）。

cron 只到分钟精度，秒忽略。
  scheduled:  M H * * * <run_task.sh> # <name>
  boot:       @reboot <run_if_due.sh> # <name>

用注释 marker 做幂等：安装时先按 marker 过滤掉旧行再追加；卸载同理。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from app.scheduler import core, paths
from app.scheduler.backends.base import Backend


def _default_run(args, input_text=None):
    return subprocess.run(args, capture_output=True, text=True, input=input_text)


class LinuxBackend(Backend):
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
            wrapper = core.write_sh(
                launcher, paths.SCHEDULER_DIR / "run_task.sh", launcher.task_argv()
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
