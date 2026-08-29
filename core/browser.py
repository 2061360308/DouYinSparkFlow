import os, sys
import subprocess
import traceback
from playwright.sync_api import sync_playwright
from utils.config import DEBUG, get_environment, Environment

PLAYWRIGHT_BROWSERS_PATH = "../chrome"

# 真实的 Chrome 桌面版 UA，避免抖音根据 HeadlessChrome 标识判定为自动化而踢下线
# 使用 Linux Chrome UA，与服务端运行平台保持一致
REAL_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.7632.6 Safari/537.36"
)

# 反自动化检测参数
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-background-networking",
]


def install_browser():
    """
    安装 Chromium 浏览器
    """
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
        print("浏览器安装完成，请重新运行程序。")
    except subprocess.CalledProcessError as e:
        print(f"发生未知错误：{e}")


def get_browser():
    """
    启动浏览器实例
    :return: 浏览器实例
    """

    headless = True

    env = get_environment()
    if env == Environment.LOCAL:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
            os.path.join(os.path.dirname(__file__), PLAYWRIGHT_BROWSERS_PATH)
        )
        # 服务器为无头环境，始终使用 headless；仅在显式提供 DISPLAY 时才允许有头调试
        if DEBUG and os.getenv("DISPLAY"):
            headless = False
    elif env == Environment.PACKED:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
            os.path.join(os.path.dirname(sys.executable), PLAYWRIGHT_BROWSERS_PATH)
        )

    try:
        # 启动浏览器
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        return playwright, browser
    except Exception as e:
        # 捕获浏览器启动错误
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            print("浏览器可执行文件不存在！")
            install_browser()
            sys.exit(1)
        else:
            traceback.print_exc()


def new_context(browser):
    """
    创建带反检测设置的浏览器上下文：
    - 使用真实 Chrome UA，避免 HeadlessChrome 被识别
    - 隐藏 navigator.webdriver 自动化标记
    - 设定中文环境与上海时区，与账号使用环境一致
    """
    context = browser.new_context(
        user_agent=REAL_USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    # 隐藏自动化标记，降低被风控识别的概率
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
        window.chrome = window.chrome || { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """
    )
    return context
