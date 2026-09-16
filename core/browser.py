import os, sys
import subprocess
import traceback
# from playwright.sync_api import sync_playwright
from cloakbrowser import launch
from utils.config import DEBUG, get_config

PLAYWRIGHT_BROWSERS_PATH = "../chrome"

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
        # 启动浏览器
        # playwright = sync_playwright().start() 
        # browser = playwright.chromium.launch(headless=headless)
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
