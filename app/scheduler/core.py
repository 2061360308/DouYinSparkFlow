"""平台无关的核心：生成启动脚本、执行任务、开机补跑判定。"""

from __future__ import annotations

import shlex
import subprocess
from datetime import date

from app.scheduler import paths, state
from app.scheduler.launcher import Launcher
from app.scheduler.lock import LockBusy, RunLock


def ensure_dirs() -> None:
    paths.SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
    paths.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _write_text(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" 表示不转换换行：内容里已按平台写好了 \r\n / \n
    path.write_text(content, encoding="utf-8", newline="")


def _vbs_command(argv: list) -> str:
    """把 argv 拼成能塞进 VBS 字符串的命令行（引号翻倍转义）。"""
    parts = [f'"{arg}"' if (" " in arg or "\t" in arg) else arg for arg in argv]
    return " ".join(parts).replace('"', '""')


def write_vbs(launcher: Launcher, path, argv: list):
    """写一个隐藏运行的 VBS 启动器（WScript，无控制台窗口）。"""
    content = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{launcher.root}"\r\n'
        f'sh.Run "{_vbs_command(argv)}", 0, False\r\n'
    )
    _write_text(path, content)
    return path


def write_sh(launcher: Launcher, path, argv: list):
    """写一个 POSIX sh 启动器（输出重定向到 logs/scheduler.log）。"""
    cmd = " ".join(shlex.quote(arg) for arg in argv)
    content = (
        "#!/bin/sh\n"
        f'cd "{launcher.root}" || exit 1\n'
        f'exec {cmd} >> "{paths.LOG_FILE}" 2>&1\n'
    )
    _write_text(path, content)
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


def run_task(launcher: Launcher | None = None) -> int:
    """同步执行一轮任务，返回进程退出码。输出追加到 logs/scheduler.log。"""
    launcher = launcher or Launcher.detect()
    ensure_dirs()
    with open(paths.LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(f"\n===== {date.today().isoformat()} 由调度器触发 =====\n")
        fh.flush()
        proc = subprocess.run(
            launcher.task_argv(),
            cwd=str(launcher.root),
            stdout=fh,
            stderr=subprocess.STDOUT,
        )
    return int(proc.returncode)


def run_if_due(*, force: bool = False, launcher: Launcher | None = None) -> dict:
    """开机补跑判定：今天已成功跑过就跳过，否则执行一轮。

    仅当退出码为 0 才记「今日已成功执行」；失败保留，下次开机还会重试。
    """
    today = date.today().isoformat()
    if not force and state.succeeded_today(today):
        return {"ran": False, "reason": "今天已成功执行", "exit_code": 0}

    lock = RunLock(paths.LOCK_FILE)
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
