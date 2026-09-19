import os
import sys
import traceback
from pathlib import Path

from cloakbrowser import launch
from utils.config import DEBUG, get_config

# 浏览器二进制的定位走 cloakbrowser 的约定（不是 Playwright 的 PLAYWRIGHT_BROWSERS_PATH）：
#   CLOAKBROWSER_BINARY_PATH  二进制绝对路径
#   CLOAKBROWSER_AUTO_UPDATE  是否允许自更新
# Docker 里由 Dockerfile 的 ENV 统一给出（/opt/cloakbrowser/chrome）；
# 本地没设时回落到仓库自带的 chrome/ 目录。用 setdefault 是为了不覆盖 Docker 的值。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCAL_CHROME = _REPO_ROOT / "chrome" / ("chrome.exe" if os.name == "nt" else "chrome")
if "CLOAKBROWSER_BINARY_PATH" not in os.environ and _LOCAL_CHROME.exists():
    os.environ["CLOAKBROWSER_BINARY_PATH"] = str(_LOCAL_CHROME)
os.environ.setdefault("CLOAKBROWSER_AUTO_UPDATE", "false")


def get_browser(fingerprint=None):
    """
    启动浏览器实例
    :return: 浏览器实例
    """
    proxyAddress = get_config()["proxyAddress"]
    headless = not DEBUG
    
    BASE_CHROME_ARGS = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-popup-blocking",
        "--disable-background-networking",
        "--metrics-recording-only",
        "--ignore-gpu-blocklist",
        "--disable-gpu",
        "--enable-unsafe-swiftshader",
    ]
    
    if fingerprint:
        BASE_CHROME_ARGS.append(f"--fingerprint={str(fingerprint)}")

    try:
        # 启动浏览器（cloakbrowser 自带 humanize 拟人化，调用方不要再叠加延迟）
        if proxyAddress:
            browser = launch(proxy=proxyAddress, headless=headless, humanize=True, args=BASE_CHROME_ARGS)
        else:
            browser = launch(headless=headless, humanize=True, args=BASE_CHROME_ARGS)
        return browser
    except Exception as e:
        # 捕获浏览器启动错误
        if "Executable doesn't exist" in str(e):
            print("浏览器可执行文件不存在！请安装CloakBrowser")
            sys.exit(1)
        else:
            traceback.print_exc()
