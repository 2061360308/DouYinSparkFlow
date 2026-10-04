"""把自带 Chromium 当成 webview，打开本地构建的网页界面。

启动顺序：
  1. 解析界面地址（APP_UI_URL 优先，否则构建产物 file://…/web/dist/index.html）
  2. 用 playwright.launch_persistent_context（有头、独立 user_data_dir），
     传 --app=<url> 让窗口没有标签栏/地址栏 —— 长成一个「应用」
  3. context.expose_function("$py", bridge.call)：Python 方法注册进页面
  4. 主循环：把 bridge 队列里的事件用 page.evaluate 推给页面；窗口关闭即退出

窗口记忆：尺寸/位置/是否最大化存在程序目录的 window.json 里，下次启动沿用；
最小尺寸用 CDP 兜底限制（Chromium 没有直接的最小尺寸开关）。

注意：sync 版 playwright 只在 API 调用时泵消息 —— 主循环里必须用
page.wait_for_timeout() 而不是 time.sleep()，否则页面调 $py 不会被执行。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

from app import paths
from app.web.bridge import Bridge
from app.web.service import make_bridge

DEFAULT_WIDTH = 850
DEFAULT_HEIGHT = 500
MIN_WIDTH = 500
MIN_HEIGHT = 450

UI_PROFILE_DIR = ".ui-profile"
WINDOW_STATE_FILE = paths.APP_DIR / "window.json"


def resolve_ui_url() -> str:
    """开发期可用 APP_UI_URL 指到 Vite dev server；否则用构建产物。"""
    override = os.getenv("APP_UI_URL", "").strip()
    if override:
        return override.rstrip("/")

    candidates = [
        paths.resource_dir() / "app" / "web" / "dist" / "index.html",
        paths.app_dir() / "web" / "dist" / "index.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve().as_uri()

    nearest = candidates[0]
    raise FileNotFoundError(
        f"找不到网页界面构建产物 {nearest}。"
        "请在 app/web/ui 里执行 npm run build（或设 APP_UI_URL 指向 dev server）。"
    )


# ---------------------------------------------------------------------------
# 窗口尺寸 / 位置的记忆
# ---------------------------------------------------------------------------
def load_window_state() -> dict:
    """读取上次的窗口状态；坏文件退回空（用默认尺寸）。"""
    try:
        data = json.loads(WINDOW_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_window_state(state: dict) -> None:
    try:
        WINDOW_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        pass


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


def _enter_app_mode() -> list:
    return [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-timer-throttling",
    ]


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


def _enforce_min_size(cdp) -> None:
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


def _capture_window_state(cdp):
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


def _apply_saved_bounds(cdp) -> None:
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


def _launch_args(url: str) -> list:
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
    args += _enter_app_mode()
    return args


def run() -> int:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    url = resolve_ui_url()
    browser = paths.browser_binary()
    if not browser.is_file():
        raise RuntimeError(f"未找到自带浏览器：{browser}")

    profile_dir = paths.APP_DIR / UI_PROFILE_DIR
    profile_dir.mkdir(parents=True, exist_ok=True)

    bridge = Bridge()
    ops = make_bridge(bridge)

    # 首次启动默认注册「常驻定时」任务。
    # 自测模式（APP_SELFTEST_SECONDS>0）或 APP_SCHEDULE_AUTOREGISTER=0 时不注册。
    try:
        selftest_env = float(os.getenv("APP_SELFTEST_SECONDS", "0") or 0)
    except ValueError:
        selftest_env = 0.0
    if selftest_env <= 0:
        try:
            from app.scheduler import api as scheduler_api

            scheduler_api.ensure_default_mode()
        except Exception as exc:
            print(f"调度任务初始化失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            executable_path=str(browser),
            args=_launch_args(url),
            no_viewport=True,
            # 不以自动化模式启动：去掉 --enable-automation 既消除了
            # 「Chrome 正在受自动软件的控制」横幅，navigator.webdriver 也变回 false
            # （对后续抓抖音登录态更隐蔽）。接口调用（$py / expose_function /
            # __pyOn）走 CDP，不依赖这个开关。
            #
            # 沙箱开启：chromium_sandbox=True 让 playwright 不自作主张加 --no-sandbox，
            # 从而消除「不受支持的命令行标记：--no-sandbox」横幅。
            chromium_sandbox=True,
            ignore_default_args=["--enable-automation"],
        )

        # --app 会多开一个应用窗口；把 playwright 自带的 about:blank 关掉，
        # 只留界面窗口（两个窗口并排很碍眼）
        for page in list(context.pages):
            if not page.url or page.url == "about:blank":
                page.close()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="load")

        # 桥必须等页面加载完再暴露：先导航后暴露的绑定才是真函数
        # （先暴露后导航时 window.$py 会是个坏桩，调用即报错）
        context.expose_function("$py", bridge.call)

        # CDP 会话用于读写窗口尺寸（Chromium 没有最小尺寸开关，只能兜底钳制）
        cdp = None
        try:
            cdp = context.new_cdp_session(page)
            _apply_saved_bounds(cdp)
        except Exception as exc:
            print(f"CDP 会话创建失败（窗口记忆不可用）：{type(exc).__name__}: {exc}", file=sys.stderr)

        print(f"app 已启动：{url}", file=sys.stderr)
        selftest = float(os.getenv("APP_SELFTEST_SECONDS", "0") or 0)
        started = time.monotonic()
        loop_count = 0
        window_state_cache = None
        try:
            while True:
                # 先处理浏览器会话（登录/拉会话）转发来的事件，再统一推给页面
                ops.pump()
                for event, data in bridge.drain():
                    expression = (
                        "window.__pyOn && "
                        f"window.__pyOn({json.dumps(event, ensure_ascii=False)}, "
                        f"{json.dumps(data, ensure_ascii=False)})"
                    )
                    try:
                        page.evaluate(expression)
                    except Exception:
                        pass  # 页面已关，事件自然丢弃
                try:
                    page.wait_for_timeout(120)
                except Exception:
                    break  # 页面/浏览器已关闭
                if page.is_closed():
                    break
                # 约每秒：钳最小尺寸 + 缓存窗口状态（关窗前读不到，所以边跑边记）
                loop_count += 1
                if cdp is not None and loop_count % 8 == 0:
                    _enforce_min_size(cdp)
                    window_state_cache = _capture_window_state(cdp)
                # 自测钩子：设置 APP_SELFTEST_SECONDS 后运行到点自动关窗退出
                if selftest and time.monotonic() - started >= selftest:
                    try:
                        page.close()
                    except Exception:
                        pass
                    break
        finally:
            # 记住窗口状态（自测不落盘，免得覆盖用户设置）
            if cdp is not None and selftest <= 0:
                captured = window_state_cache
                if captured is None:
                    captured = _capture_window_state(cdp)
                if captured is not None:
                    if captured.get("maximized"):
                        saved = load_window_state()
                        saved["maximized"] = True
                        save_window_state(saved)
                    else:
                        save_window_state(captured)
            try:
                context.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
