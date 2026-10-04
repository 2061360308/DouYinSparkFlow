"""调度核心：启动器推导、启动脚本生成、运行任务、开机补跑判定、运行锁。

平台无关；具体「注册到哪个系统任务」在 backends.py。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app import paths
from app.config import env_store
from app.scheduler import state
from app.util import no_window_kwargs

# 计划任务名（Windows schtasks /TN；Linux cron 用它的注释 marker）
DEFAULT_NAME = "DouYinSparkFlow"

# 每天执行的默认时刻（.env 没写 CRON_* 时用）
DEFAULT_RUN_TIME = "09:00"


# --------------------------------------------------------------------------- 启动器
@dataclass
class Launcher:
    """把「跑一轮任务 / 跑调度器自身」翻译成当前运行形态下可执行的 argv。

    源码运行： [python, <root>/main.py, task]
    exe 运行： [<exe>, task]                 （sys.frozen 为真时）

    不写死解释器：源码下 sys.executable 就是 python，打包后就是 exe 自身。
    """

    exe: str
    frozen: bool
    root: Path

    @classmethod
    def detect(cls) -> "Launcher":
        return cls(exe=sys.executable, frozen=paths.FROZEN, root=paths.ROOT_DIR)

    @classmethod
    def for_exe(cls, exe_path: str) -> "Launcher":
        """显式指定一个 exe（例如给已打包的程序注册任务）。"""
        resolved = Path(exe_path).expanduser().resolve()
        return cls(exe=str(resolved), frozen=True, root=resolved.parent)

    @classmethod
    def for_python(cls, python_path: str) -> "Launcher":
        """显式指定 python 解释器（源码形态）。"""
        return cls(exe=str(Path(python_path).expanduser()), frozen=False, root=paths.ROOT_DIR)

    def task_argv(self) -> list:
        """执行一轮任务。"""
        if self.frozen:
            return [self.exe, "task"]
        return [self.exe, str(self.root / "main.py"), "task"]

    def scheduler_argv(self, *args: str) -> list:
        """调度器自身入口（如 run-if-due）。"""
        if self.frozen:
            return [self.exe, "scheduler", *args]
        return [self.exe, str(self.root / "main.py"), "scheduler", *args]


# --------------------------------------------------------------------------- 配置
def _clamp(raw, low: int, high: int, fallback: int) -> int:
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def resolve_run_time(env: dict | None = None) -> str:
    """返回 HH:MM。取 .env 的 CRON_HOUR/CRON_MINUTE，秒忽略（cron 只到分钟）。"""
    if env is None:
        env = env_store.read_env_map(paths.ENV_FILE)
    hour = _clamp(env.get("CRON_HOUR"), 0, 23, 9)
    minute = _clamp(env.get("CRON_MINUTE"), 0, 59, 0)
    return f"{hour:02d}:{minute:02d}"


# --------------------------------------------------------------------------- 目录/脚本
def ensure_dirs() -> None:
    paths.SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
    paths.SCHEDULER_LOG.parent.mkdir(parents=True, exist_ok=True)


def _write_text(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" 表示不转换换行：内容里已按平台写好了 \r\n / \n
    path.write_text(content, encoding="utf-8", newline="")


def _vbs_command(argv: list) -> str:
    """把 argv 拼成能塞进 VBS 字符串的命令行（引号翻倍转义）。"""
    parts = [f'"{arg}"' if (" " in arg or "\t" in arg) else arg for arg in argv]
    return " ".join(parts).replace('"', '""')


def write_vbs(launcher: Launcher, path, argv: list):
    """写一个隐藏运行的 VBS 启动器（WScript，无控制台窗口）。

    自带浏览器存在时，把 CLOAKBROWSER_BINARY_PATH / CLOAKBROWSER_AUTO_UPDATE 写进
    进程环境 —— 计划任务没有交互环境，不设的话任务找不到自带 Chromium。
    """
    lines = [
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.CurrentDirectory = "{launcher.root}"',
    ]
    browser = paths.browser_binary()
    if browser.is_file():
        lines.append(
            f'sh.Environment("PROCESS")("CLOAKBROWSER_BINARY_PATH") = "{browser}"'
        )
        lines.append('sh.Environment("PROCESS")("CLOAKBROWSER_AUTO_UPDATE") = "false"')
    lines.append(f'sh.Run "{_vbs_command(argv)}", 0, False')
    _write_text(path, "\r\n".join(lines) + "\r\n")
    return path


def write_sh(launcher: Launcher, path, argv: list):
    """写一个 POSIX sh 启动器（输出重定向到 logs/scheduler.log）。

    自带浏览器存在时一并导出 CLOAKBROWSER_BINARY_PATH / CLOAKBROWSER_AUTO_UPDATE。
    """
    cmd = " ".join(shlex.quote(arg) for arg in argv)
    lines = [
        "#!/bin/sh",
        f'cd "{launcher.root}" || exit 1',
    ]
    browser = paths.browser_binary()
    if browser.is_file():
        lines.append(f'export CLOAKBROWSER_BINARY_PATH="{browser}"')
        lines.append('export CLOAKBROWSER_AUTO_UPDATE="false"')
    # 数据目录跟桌面端保持一致：deb 装在 /opt 时数据落在用户目录
    data_dir = os.environ.get("APP_DATA_DIR", "").strip()
    if data_dir:
        lines.append(f'export APP_DATA_DIR="{data_dir}"')
    lines.append(f'exec {cmd} >> "{paths.SCHEDULER_LOG}" 2>&1')
    _write_text(path, "\n".join(lines) + "\n")
    try:
        path.chmod(0o755)
    except OSError:
        pass
    return path


def remove_wrappers() -> None:
    for pattern in ("run_task.*", "run_if_due.*"):
        for path in paths.SCHEDULER_DIR.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------- 运行锁
class LockBusy(Exception):
    """已有任务在运行。"""


class RunLock:
    """跨平台运行锁：避免「常驻定时」与「开机执行」同时跑同一个任务。

    用 O_CREAT|O_EXCL 原子创建锁文件；锁文件很久没动（上次进程被强杀）时按陈旧锁处理。
    """

    STALE_SECONDS = 6 * 3600

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._acquired = False

    def _try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
        finally:
            os.close(fd)
        self._acquired = True
        return True

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age > self.STALE_SECONDS

    def __enter__(self) -> "RunLock":
        if self._try_acquire():
            return self
        if self._is_stale():
            try:
                self.path.unlink()
            except OSError:
                pass
            if self._try_acquire():
                return self
        raise LockBusy("已有任务在运行")

    def __exit__(self, *_exc) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._acquired = False


# --------------------------------------------------------------------------- 执行
def run_task(launcher: Launcher | None = None) -> int:
    """同步执行一轮任务，返回进程退出码。输出追加到 logs/scheduler.log。"""
    launcher = launcher or Launcher.detect()
    ensure_dirs()
    with open(paths.SCHEDULER_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {date.today().isoformat()} 由调度器触发 =====\n")
        fh.flush()
        proc = subprocess.run(
            launcher.task_argv(),
            cwd=str(launcher.root),
            stdout=fh,
            stderr=subprocess.STDOUT,
            **no_window_kwargs(),
        )
    return int(proc.returncode)


def run_if_due(*, force: bool = False, launcher: Launcher | None = None) -> dict:
    """开机补跑判定：今天已成功跑过就跳过，否则执行一轮。

    仅当退出码为 0 才记「今日已成功执行」；失败保留，下次开机还会重试。
    """
    today = date.today().isoformat()
    if not force and state.succeeded_today(today):
        return {"ran": False, "reason": "今天已成功执行", "exit_code": 0}

    lock = RunLock(paths.SCHEDULER_LOCK)
    try:
        with lock:
            code = run_task(launcher)
            state.record_attempt(code, today)
            if code == 0:
                state.record_success(today)
    except LockBusy as exc:
        return {"ran": False, "reason": str(exc), "exit_code": 0}

    reason = "已执行" if code == 0 else f"执行失败（退出码 {code}），下次开机重试"
    return {"ran": True, "reason": reason, "exit_code": code}
