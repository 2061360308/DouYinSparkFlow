"""调度模块的持久化：状态（跑了没）与安装记录（当前模式/后端）。

两份都是小 JSON，写在 .scheduler/ 下。读写一律「坏文件退回默认、绝不抛异常」，
避免一个损坏的状态文件把整个任务卡死。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.scheduler import paths


def _read_json(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- 状态
def load_state() -> dict:
    return _read_json(paths.STATE_FILE)


def save_state(data: dict) -> None:
    _write_json(paths.STATE_FILE, data)


def record_attempt(exit_code: int, today: str) -> dict:
    """记录一次尝试（无论成败）。成功日期由 record_success 单独写。"""
    state = load_state()
    state["version"] = 1
    state["last_attempt_date"] = today
    state["last_attempt_at"] = _now()
    state["last_exit_code"] = int(exit_code)
    save_state(state)
    return state


def record_success(today: str) -> dict:
    state = load_state()
    state["version"] = 1
    state["last_success_date"] = today
    state["last_success_at"] = _now()
    state["last_exit_code"] = 0
    save_state(state)
    return state


def succeeded_today(today: str) -> bool:
    return str(load_state().get("last_success_date") or "") == today


# --------------------------------------------------------------------------- 安装记录
def load_install() -> dict:
    return _read_json(paths.INSTALL_FILE)


def save_install(data: dict) -> None:
    data = dict(data or {})
    data["version"] = 1
    data["updated_at"] = _now()
    _write_json(paths.INSTALL_FILE, data)
