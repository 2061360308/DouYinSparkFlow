"""宿主窗口：启动参数（尺寸/位置/最大化）、CDP 尺寸读写、最小尺寸钳制。

Chromium 没有直接的最小尺寸开关，只能用 CDP 周期性兜底钳制；窗口状态（尺寸/
位置/是否最大化）记在程序目录的 window.json 里，下次启动沿用。
"""

from __future__ import annotations

import ctypes
import json
import time
from pathlib import Path

from app import jsonstore, paths

DEFAULT_WIDTH = 850
DEFAULT_HEIGHT = 500
MIN_WIDTH = 500
MIN_HEIGHT = 450

WINDOW_STATE_FILE = paths.APP_DIR / "window.json"


# ---------------------------------------------------------------------------
# 记忆（window.json）
# ---------------------------------------------------------------------------
def load_window_state() -> dict:
    """读取上次的窗口状态；坏文件退回空（用默认尺寸）。"""
    data = jsonstore.read_json(WINDOW_STATE_FILE)
    return data if isinstance(data, dict) else {}


def save_window_state(state: dict) -> None:
    try:
        jsonstore.write_json(WINDOW_STATE_FILE, state)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 启动参数
# ---------------------------------------------------------------------------
def _default_position(width: int, height: int) -> list:
    """默认居中；屏幕不够就贴左上角。"""
    try:
        import ctypes.wintypes

        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(
            ctypes.windll.user32.GetDesktopWindow(), ctypes.byref(rect)
        )
        screen_w = rect.right - rect.left
        screen_h = rect.bottom - rect.top
    except Exception:
        screen_w, screen_h = 1920, 1080
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    return [f"--window-position={x},{y}"]


def _app_mode_args() -> list:
    return [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-timer-throttling",
        # 界面窗口在 Linux 上以 --no-sandbox 启动（系统禁用了非特权 userns），
        # Chromium 会因此弹「不受支持的命令行标记」横幅。--test-type 让启动阶段的
        # 信息条整体跳过（见 Chromium 的 infobar_utils.cc::AddInfoBarsIfNecessary），
        # 只作用于界面窗口，不改网页加载 / 渲染 / JS 行为。
        "--test-type",
    ]


def launch_args(url: str) -> list:
    """按上次状态拼窗口参数：尺寸/位置沿用，最大化时加 --start-maximized。"""
    state = load_window_state()
    width = max(MIN_WIDTH, int(state.get("width") or DEFAULT_WIDTH))
    height = max(MIN_HEIGHT, int(state.get("height") or DEFAULT_HEIGHT))

    args = [f"--app={url}", f"--window-size={width},{height}"]
    if state.get("left") is not None and state.get("top") is not None:
        args.append(f"--window-position={int(state['left'])},{int(state['top'])}")
    else:
        args += _default_position(width, height)
    if state.get("maximized"):
        args.append("--start-maximized")
    args += _app_mode_args()
    return args


# ---------------------------------------------------------------------------
# CDP 尺寸读写
# ---------------------------------------------------------------------------
def _window_bounds(cdp):
    """返回 (windowId, bounds)；失败返回 (None, {})。"""
    try:
        info = cdp.send("Browser.getWindowForTarget")
        return info.get("windowId"), dict(info.get("bounds") or {})
    except Exception:
        return None, {}


def _set_window_bounds(cdp, window_id, bounds) -> None:
    try:
        cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
    except Exception:
        pass


def enforce_min_size(cdp) -> None:
    """把窗口撑到不小于最小尺寸（最大化/最小化时不干预）。"""
    window_id, bounds = _window_bounds(cdp)
    if window_id is None:
        return
    if bounds.get("windowState") not in (None, "normal"):
        return
    width = int(bounds.get("width") or 0)
    height = int(bounds.get("height") or 0)
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        _set_window_bounds(
            cdp,
            window_id,
            {"width": max(width, MIN_WIDTH), "height": max(height, MIN_HEIGHT)},
        )


def capture_window_state(cdp):
    """读取当前窗口状态用于落盘；失败返回 None。"""
    window_id, bounds = _window_bounds(cdp)
    if window_id is None:
        return None
    if bounds.get("windowState") == "maximized":
        return {"maximized": True}
    state = {"maximized": False}
    for key in ("left", "top", "width", "height"):
        if bounds.get(key) is not None:
            state[key] = int(bounds[key])
    if state.get("width"):
        state["width"] = max(MIN_WIDTH, state["width"])
    if state.get("height"):
        state["height"] = max(MIN_HEIGHT, state["height"])
    return state


def apply_saved_bounds(cdp) -> None:
    """启动后按上次状态精确设置窗口。

    --window-size 与 CDP setWindowBounds 对边框的处理不一致（会有 1~2px 误差），
    这里用几次反馈校正，把 outer 尺寸收敛到记忆值，避免每次启动累积漂移。
    """
    state = load_window_state()
    if not state:
        return
    window_id, _ = _window_bounds(cdp)
    if window_id is None:
        return
    if state.get("maximized"):
        _set_window_bounds(cdp, window_id, {"windowState": "maximized"})
        return

    want_w = max(MIN_WIDTH, int(state.get("width") or DEFAULT_WIDTH))
    want_h = max(MIN_HEIGHT, int(state.get("height") or DEFAULT_HEIGHT))
    left = state.get("left")
    top = state.get("top")

    for _ in range(4):
        bounds = {"width": want_w, "height": want_h}
        if left is not None:
            bounds["left"] = int(left)
        if top is not None:
            bounds["top"] = int(top)
        _set_window_bounds(cdp, window_id, bounds)
        time.sleep(0.08)
        _, current = _window_bounds(cdp)
        got_w = int(current.get("width") or want_w)
        got_h = int(current.get("height") or want_h)
        if abs(got_w - want_w) <= 1 and abs(got_h - want_h) <= 1:
            return
        want_w += want_w - got_w
        want_h += want_h - got_h
