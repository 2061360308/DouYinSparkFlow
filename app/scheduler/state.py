"""调度模块的持久化：状态（跑了没）、按天历史、安装记录（当前模式/后端）。

都是小 JSON，写在 .scheduler/ 下。读写一律「坏文件退回默认、绝不抛异常」，
避免一个损坏的文件把整个任务卡死。
"""

from __future__ import annotations

from app import jsonstore, paths
from app.util import now_str


# --------------------------------------------------------------------------- 状态
def load_state() -> dict:
    return jsonstore.read_json(paths.SCHEDULER_STATE)


def save_state(data: dict) -> None:
    jsonstore.write_json(paths.SCHEDULER_STATE, data)


def record_attempt(exit_code: int, today: str) -> dict:
    """记录一次尝试（无论成败）。成功日期由 record_success 单独写。"""
    state = load_state()
    state["version"] = 1
    state["last_attempt_date"] = today
    state["last_attempt_at"] = now_str()
    state["last_exit_code"] = int(exit_code)
    save_state(state)
    _bump_history(today, exit_code)
    return state


def record_success(today: str) -> dict:
    state = load_state()
    state["version"] = 1
    state["last_success_date"] = today
    state["last_success_at"] = now_str()
    state["last_exit_code"] = 0
    save_state(state)
    _mark_success(today)
    return state


def succeeded_today(today: str) -> bool:
    return str(load_state().get("last_success_date") or "") == today


# --------------------------------------------------------------------------- 按天历史
def load_history() -> dict:
    """返回 {日期: {attempts, success, last_exit_code, last_at}}。"""
    data = jsonstore.read_json(paths.SCHEDULER_HISTORY)
    days = data.get("days") if isinstance(data, dict) else None
    return days if isinstance(days, dict) else {}


def _save_history(days: dict) -> None:
    jsonstore.write_json(paths.SCHEDULER_HISTORY, {"version": 1, "days": days})


def _bump_history(today: str, exit_code: int) -> None:
    days = load_history()
    entry = dict(days.get(today) or {})
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["last_exit_code"] = int(exit_code)
    entry["last_at"] = now_str()
    entry.setdefault("success", False)
    days[today] = entry
    _save_history(days)


def _mark_success(today: str) -> None:
    days = load_history()
    entry = dict(days.get(today) or {})
    entry["success"] = True
    entry["last_exit_code"] = 0
    entry["last_at"] = now_str()
    entry.setdefault("attempts", 1)
    days[today] = entry
    _save_history(days)


def history_days() -> dict:
    return load_history()


# --------------------------------------------------------------------------- 安装记录
def load_install() -> dict:
    return jsonstore.read_json(paths.SCHEDULER_INSTALL)


def save_install(data: dict) -> None:
    data = dict(data or {})
    data["version"] = 1
    data["updated_at"] = now_str()
    jsonstore.write_json(paths.SCHEDULER_INSTALL, data)
