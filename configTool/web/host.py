"""把自带 Chromium 当成 webview，打开本地构建的网页界面。

启动顺序：
  1. 解析界面地址（CONFIGTOOL_UI_URL 优先，否则构建产物 file://…/web/dist/index.html）
  2. 用 playwright.launch_persistent_context（有头、独立 user_data_dir），
     传 --app=<url> 让窗口没有标签栏/地址栏 —— 长成一个「应用」
  3. context.expose_function("$py", bridge.call)：Python 方法注册进页面
  4. 主循环：把 bridge 队列里的事件用 page.evaluate 推给页面；窗口关闭即退出

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

from configTool import paths
from configTool.web.bridge import Bridge
from configTool.web.service import make_bridge

APP_WIDTH = 1280
APP_HEIGHT = 800
UI_PROFILE_DIR = ".ui-profile"


def resolve_ui_url() -> str:
    """开发期可用 CONFIGTOOL_UI_URL 指到 Vite dev server；否则用构建产物。"""
    override = os.getenv("CONFIGTOOL_UI_URL", "").strip()
    if override:
        return override.rstrip("/")

    candidates = [
        paths.resource_dir() / "configTool" / "web" / "dist" / "index.html",
        paths.app_dir() / "web" / "dist" / "index.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve().as_uri()

    nearest = candidates[0]
    raise FileNotFoundError(
        f"找不到网页界面构建产物 {nearest}。"
        "请在 configTool/web/ui 里执行 npm run build（或设 CONFIGTOOL_UI_URL 指向 dev server）。"
    )


def _window_geometry() -> list:
    """返回 --window-position 参数。默认 1280x800 居中，屏幕不够就贴左上角。"""
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
    x = max(0, (screen_w - APP_WIDTH) // 2)
    y = max(0, (screen_h - APP_HEIGHT) // 3)
    return [f"--window-position={x},{y}"]


def _enter_app_mode() -> list:
    args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-infobars",
        "--disable-background-timer-throttling",
    ]
    args += _window_geometry()
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
    make_bridge(bridge)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            executable_path=str(browser),
            args=[f"--app={url}", f"--window-size={APP_WIDTH},{APP_HEIGHT}"] + _enter_app_mode(),
            no_viewport=True,
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

        print(f"configTool 已启动：{url}", file=sys.stderr)
        selftest = float(os.getenv("CONFIGTOOL_SELFTEST_SECONDS", "0") or 0)
        started = time.monotonic()
        try:
            while True:
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
                # 自测钩子：设置 CONFIGTOOL_SELFTEST_SECONDS 后运行到点自动关窗退出
                if selftest and time.monotonic() - started >= selftest:
                    try:
                        page.close()
                    except Exception:
                        pass
                    break
        finally:
            try:
                context.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run())