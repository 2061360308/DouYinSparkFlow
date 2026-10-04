"""原子 JSON 读写：坏文件退回默认、写盘先临时文件再替换。

`config/settings.py`、`config/profile_store.py`、`scheduler/state.py` 共用，
避免三处各写一遍。
"""

from __future__ import annotations

import json
from pathlib import Path


def read_json(path, default=None, errors=None):
    """读取 JSON；文件不存在或内容坏了返回 default（默认 {}）。

    errors 传 list 时，解析失败会把原因 append 进去（供界面提示）。
    """
    target = Path(path)
    if not target.is_file():
        return {} if default is None else default
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        if errors is not None:
            errors.append(f"{target.name} 读取失败（{type(exc).__name__}: {exc}）")
        return {} if default is None else default
    return data


def write_json(path, data) -> None:
    """整表写回：先写临时文件再替换，避免中途崩掉留下半个文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(target)
