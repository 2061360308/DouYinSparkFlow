"""桥：把 Python 方法注册进页面（window.$py），支持 Python 主动推送（__pyOn）。

页面侧用法（ui/src/api.js）：
    const res = await window.$py("get_config", null)   // 返回值已 JSON 化
    window.__pyOn("event", (data) => { ... })          // Python 主动推送

本文件只负责「传输 + 方法表」，业务逻辑在 service.py。
Playwright 的 expose_function 走 CDP 内置绑定，不经过 HTTP / 端口 / 轮询。
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable

from app.errors import AppError

_JSON_SAFE = (dict, list, str, int, float, bool, type(None))


def _json_safe(value, path="返回值"):
    if isinstance(value, _JSON_SAFE):
        if isinstance(value, dict):
            return {str(k): _json_safe(v, path) for k, v in value.items()}
        if isinstance(value, list):
            return [_json_safe(item, path) for item in value]
        return value
    # 不是 JSON 安全类型就报错，别让 playwright 序列化蹦出奇怪的东西
    raise TypeError(f"{path}含不支持的类型 {type(value).__name__}")


class Bridge:
    """方法注册表 + 双向通道。线程安全。"""

    def __init__(self) -> None:
        self._methods: dict[str, Callable] = {}
        self._events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 注册
    def register(self, name: str, handler: Callable) -> None:
        if not name.isidentifier():
            raise ValueError(f"方法名必须是非空标识符：{name!r}")
        self._methods[name] = handler

    def register_all(self, **handlers) -> None:
        for name, handler in handlers.items():
            self.register(name, handler)

    # ------------------------------------------------------- 页面 -> Python
    def call(self, method, payload=None) -> dict:
        """暴露给页面的唯一入口。返回值必须能 JSON 序列化。

        无论成败都返回 dict：失败带 {"error": ...}，由前端 reject。
        """
        try:
            if not isinstance(method, str) or not method:
                raise ValueError("method 必须是字符串")
            with self._lock:
                handler = self._methods.get(method)
            if handler is None:
                raise KeyError(f"未注册的方法：{method}")
            result = handler(payload)
            return _json_safe(result)
        except AppError as exc:
            # 面向用户的业务错误：消息原样回传
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 任何异常都要回给页面
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------- Python -> 页面
    def emit(self, event: str, data=None) -> None:
        """线程安全地入队一个事件，由宿主主循环取走后 page.evaluate 推送。"""
        self._events.put((event, _json_safe(data, f"事件 {event}")))

    def drain(self) -> list:
        """取走当前所有待推送事件（主循环每轮调用一次）。"""
        pending: list = []
        while True:
            try:
                pending.append(self._events.get_nowait())
            except queue.Empty:
                break
        return pending