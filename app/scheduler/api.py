"""调度模块的高层 API：查询状态、切换模式、开机补跑、卸载。

CLI 与 app 都调用这里，保证两边行为一致。
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from app.scheduler import core, state
from app.scheduler.backends import get_backend
from app.scheduler.core import Launcher

MODE_SCHEDULED = "scheduled"  # 常驻定时
MODE_BOOT = "boot"            # 开机执行
MODE_CONFIG = "config"        # 生成配置

MODES = (MODE_SCHEDULED, MODE_BOOT, MODE_CONFIG)

MODE_LABELS = {
    MODE_SCHEDULED: "常驻定时",
    MODE_BOOT: "开机执行",
    MODE_CONFIG: "生成配置",
}


def _task_name(name: str | None = None) -> str:
    return (name or state.load_install().get("name") or core.DEFAULT_NAME).strip()


def _normalize_time(value: str | None) -> str:
    """统一成 HH:MM。主程序传来的可能是 HH:MM:SS，秒忽略。"""
    parts = str(value or "").split(":")
    if len(parts) >= 2:
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except (TypeError, ValueError):
            pass
    return core.resolve_run_time()


def get_status() -> dict:
    """当前模式、是否已注册、后端信息。"""
    install = state.load_install()
    mode = str(install.get("mode") or "")
    name = _task_name(install.get("name"))
    backend = get_backend()
    available, why = backend.available()

    installed = False
    if mode in (MODE_SCHEDULED, MODE_BOOT) and available:
        try:
            installed = bool(backend.status(name=name, mode=mode).get("installed"))
        except Exception:
            installed = False

    days = state.history_days()
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    return {
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode, ""),
        "name": name,
        "backend": backend.name,
        "installed": installed,
        "available": available,
        "unavailable_reason": why,
        "run_time": str(install.get("run_time") or ""),
        "modes": list(MODES),
        "mode_labels": dict(MODE_LABELS),
        "run": {
            "today": days.get(today),
            "yesterday": days.get(yesterday),
        },
    }


def history() -> dict:
    """按天执行历史（供热力图）：{日期: {attempts, success, last_exit_code, last_at}}。"""
    return state.history_days()


def set_mode(
    mode: str,
    *,
    run_time: str | None = None,
    name: str | None = None,
    launcher: Launcher | None = None,
    backend=None,
) -> dict:
    """切换模式：先卸载旧任务，再按新模式注册（config 只卸载）。

    后端不可用或注册失败时抛异常，且尽量回滚到原模式，避免「任务没了、模式还写着旧的」。
    """
    mode = str(mode or "").strip()
    if mode not in MODES:
        raise ValueError(f"未知模式：{mode!r}（可选：{', '.join(MODES)}）")

    launcher = launcher or Launcher.detect()
    backend = backend or get_backend()
    name = _task_name(name)

    available, why = backend.available()
    if mode in (MODE_SCHEDULED, MODE_BOOT) and not available:
        raise RuntimeError(f"无法注册系统任务：{why}")

    previous = state.load_install()
    run_time = _normalize_time(run_time)

    # 卸载旧任务 + 清掉旧包装脚本
    try:
        backend.uninstall(name=name)
    except Exception:
        pass
    core.remove_wrappers()

    if mode == MODE_SCHEDULED:
        try:
            backend.install(name=name, mode=mode, run_time=run_time, launcher=launcher)
        except Exception:
            _restore(previous, launcher, backend, name)
            raise
    elif mode == MODE_BOOT:
        try:
            backend.install(name=name, mode=mode, run_time=run_time, launcher=launcher)
        except Exception:
            _restore(previous, launcher, backend, name)
            raise
    # MODE_CONFIG：不注册

    state.save_install(
        {
            "mode": mode,
            "name": name,
            "run_time": run_time,
            "backend": backend.name,
        }
    )
    return get_status()


def _restore(previous: dict, launcher: Launcher, backend, name: str) -> None:
    """注册失败时，尽力把之前的模式装回去。"""
    old_mode = str(previous.get("mode") or "")
    if old_mode not in (MODE_SCHEDULED, MODE_BOOT):
        return
    try:
        old_time = str(previous.get("run_time") or core.resolve_run_time())
        backend.install(name=name, mode=old_mode, run_time=old_time, launcher=launcher)
        state.save_install(previous)
    except Exception:
        pass


def uninstall(*, name: str | None = None, backend=None) -> dict:
    """卸载系统任务（幂等），模式清空。"""
    backend = backend or get_backend()
    name = _task_name(name)
    try:
        backend.uninstall(name=name)
    except Exception:
        pass
    core.remove_wrappers()
    state.save_install({"mode": "", "name": name})
    return get_status()


def cancel(*, name: str | None = None, backend=None) -> dict:
    """只移除系统注册（任务 + 启动脚本），**保留当前模式** —— 便于「重新注册」。

    与 uninstall 的区别：不清空 install.json 里的 mode。
    """
    backend = backend or get_backend()
    name = _task_name(name)
    try:
        backend.uninstall(name=name)
    except Exception:
        pass
    core.remove_wrappers()
    return get_status()


def ensure_default_mode() -> dict:
    """首次启动（尚无模式记录）时，默认注册「常驻定时」。

    设 APP_SCHEDULE_AUTOREGISTER=0 可关闭（测试 / 冒烟用）。
    """
    if os.getenv("APP_SCHEDULE_AUTOREGISTER", "1").strip() == "0":
        return get_status()
    if state.load_install().get("mode"):
        return get_status()
    try:
        return set_mode(MODE_SCHEDULED, run_time=core.resolve_run_time())
    except Exception:
        # 注册失败不该拖垮启动；状态里保持未注册
        return get_status()


def run_if_due(*, force: bool = False, launcher: Launcher | None = None) -> dict:
    """开机补跑（供包装脚本调用）。"""
    return core.run_if_due(force=force, launcher=launcher)
