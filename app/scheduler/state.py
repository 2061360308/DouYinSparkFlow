"""调度模块的持久化：状态（跑了没）与安装记录（当前模式/后端）。

两份都是小 JSON，写在 .scheduler/ 下。读写一律「坏文件退回默认、绝不抛异常」，
避免一个损坏的状态文件把整个任务卡死。
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
    return state


def record_success(today: str) -> dict:
    state = load_state()
    state["version"] = 1
    state["last_success_date"] = today
    state["last_success_at"] = now_str()
    state["last_exit_code"] = 0
    save_state(state)
    return state


def succeeded_today(today: str) -> bool:
    return str(load_state().get("last_success_date") or "") == today


# --------------------------------------------------------------------------- 安装记录
def load_install() -> dict:
    return jsonstore.read_json(paths.SCHEDULER_INSTALL)


def save_install(data: dict) -> None:
    data = dict(data or {})
    data["version"] = 1
    data["updated_at"] = now_str()
    jsonstore.write_json(paths.SCHEDULER_INSTALL, data)
