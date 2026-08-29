import os
import sys
import subprocess
import traceback
from playwright.sync_api import sync_playwright
from utils.config import DEBUG, get_environment, Environment

PLAYWRIGHT_BROWSERS_PATH = "../chrome"
CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]


def install_browser():
    """
    安装 Chromium 浏览器
    """
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
        print("浏览器安装完成，请重新运行程序。")
    except subprocess.CalledProcessError as e:
        print(f"发生未知错误：{e}")


def _maybe_use_bundled_chrome(path):
    if os.path.isdir(path):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path


def get_browser():
    """
    启动浏览器实例
    :return: 浏览器实例
    """

    headless = True

    env = get_environment()
    if env == Environment.LOCAL:
        _maybe_use_bundled_chrome(
            os.path.abspath(os.path.join(os.path.dirname(__file__), PLAYWRIGHT_BROWSERS_PATH))
        )
        if DEBUG:
            headless = False
    elif env == Environment.PACKED:
        _maybe_use_bundled_chrome(
            os.path.abspath(os.path.join(os.path.dirname(sys.executable), PLAYWRIGHT_BROWSERS_PATH))
        )

    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless, args=CHROMIUM_ARGS)
        return playwright, browser
    except Exception as e:
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            print("浏览器可执行文件不存在！")
            install_browser()
            sys.exit(1)
        traceback.print_exc()
        raise
