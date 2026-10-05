"""把自带 Chromium 当成 webview，打开本地构建的网页界面。

启动顺序：
  1. 解析界面地址（APP_UI_URL 优先，否则构建产物 file://…/web/dist/index.html）
  2. 用 playwright.launch_persistent_context（有头、独立 user_data_dir），
     传 --app=<url> 让窗口没有标签栏/地址栏 —— 长成一个「应用」
  3. context.expose_function("$py", bridge.call)：Python 方法注册进页面
  4. 主循环：把 bridge 队列里的事件用 page.evaluate 推给页面；窗口关闭即退出

窗口尺寸/位置/最大化与最小尺寸钳制见 window.py。

注意：sync 版 playwright 只在 API 调用时泵消息 —— 主循环里必须用
page.wait_for_timeout() 而不是 time.sleep()，否则页面调 $py 不会被执行。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time

from app import paths
from app.web import window
from app.web.bridge import Bridge
from app.web.service import make_bridge

UI_PROFILE_DIR = ".ui-profile"


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
            args=window.launch_args(url),
            no_viewport=True,
            # 不以自动化模式启动：去掉 --enable-automation 既消除了
            # 「Chrome 正在受自动软件的控制」横幅，navigator.webdriver 也变回 false
            # （对后续抓抖音登录态更隐蔽）。接口调用（$py / expose_function /
            # __pyOn）走 CDP，不依赖这个开关。
            #
            # 关闭沙箱：Ubuntu 23.10+/24.04 等发行版默认用 AppArmor 禁用了非特权
            # user namespace，Chromium 自带的沙箱起不来（No usable sandbox），
            # 只能 --no-sandbox。cloakbrowser 的隐身二进制本身也默认 --no-sandbox
            # （任务路径就是），这里保持一致。Windows 上该开关无影响。
            chromium_sandbox=False,
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
            window.apply_saved_bounds(cdp)
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
                    window.enforce_min_size(cdp)
                    window_state_cache = window.capture_window_state(cdp)
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
                    captured = window.capture_window_state(cdp)
                if captured is not None:
                    if captured.get("maximized"):
                        saved = window.load_window_state()
                        saved["maximized"] = True
                        window.save_window_state(saved)
                    else:
                        window.save_window_state(captured)
            try:
                context.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
