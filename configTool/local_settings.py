"""本工具自己的本地设置（local.json）。

为什么不写进 .env：
    .env 是给主程序（上游还有云函数）吃的配置文件，工具私有的键塞进去既会污染主程序配置，
    也会被一起带到云函数上。所以单独落一份 JSON，跟着程序目录走。

目前只存一件事 —— 抓 Cookie 时要不要走云函数侧的 gost 隧道：

    {
      "version": 1,
      "proxy": {
        "enabled": true,
        "tunnel": "wss://xxxx.cn-hangzhou.fcapp.run:443?path=/ws",
        "user": "gost",
        "password": "...",
        "gost_path": ""
      }
    }

``tunnel`` 里**不要**写用户名密码（工具会自己插进去并做转义），只写地址和参数。
``gost_path`` 留空表示自动查找：程序目录 → 程序目录/gost → 程序目录/bin。

安全提醒：密码是明文落在这份文件里的，它只该待在本机程序目录，不要连同工具一起发给别人。
"""

from __future__ import annotations

import json
from pathlib import Path

from configTool import paths

SETTINGS_FILE = paths.LOCAL_SETTINGS
VERSION = 1

DEFAULT_PROXY = {
    "enabled": False,
    "tunnel": "",
    "user": "",
    "password": "",
    # 留空 = 自动查找 gost.exe
    "gost_path": "",
}

_KEYS = ("tunnel", "user", "password", "gost_path")


def _clean_proxy(raw) -> dict:
    """归一化代理配置：缺字段补默认值，多余字段丢掉。"""
    data = dict(DEFAULT_PROXY)
    if isinstance(raw, dict):
        data["enabled"] = bool(raw.get("enabled", False))
        for key in _KEYS:
            data[key] = str(raw.get(key) or "").strip()
    return data


def load() -> dict:
    """读设置。文件不存在或内容坏了都退回默认值（绝不抛异常）。"""
    path = Path(SETTINGS_FILE)
    empty = {"version": VERSION, "proxy": dict(DEFAULT_PROXY)}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    return {"version": VERSION, "proxy": _clean_proxy(data.get("proxy"))}


def save(settings) -> None:
    """整表写回（先写临时文件再替换，避免中途崩掉留下半个文件）。"""
    path = Path(SETTINGS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": VERSION,
        "proxy": _clean_proxy((settings or {}).get("proxy")),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def save_proxy(proxy) -> None:
    """只更新代理那一块，其余设置原样保留。"""
    settings = load()
    settings["proxy"] = _clean_proxy(proxy)
    save(settings)


def proxy_config() -> dict:
    return load()["proxy"]


def proxy_ready(proxy) -> tuple:
    """检查代理配置能不能用来起隧道，返回 (是否可用, 说明)。"""
    proxy = _clean_proxy(proxy)
    if not proxy["enabled"]:
        return False, "未启用配套代理"
    if not proxy["tunnel"]:
        return False, "没有填隧道地址"
    if not proxy["tunnel"].startswith(("ws://", "wss://")):
        return False, "隧道地址要以 ws:// 或 wss:// 开头"
    return True, ""
