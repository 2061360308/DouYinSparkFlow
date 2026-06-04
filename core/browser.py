import os, sys
import subprocess
import traceback
from rich.console import Console
from playwright.async_api import async_playwright
from utils.config import DEBUG, get_environment, Environment

PLAYWRIGHT_BROWSERS_PATH = "../chrome"

# 初始化 rich 控制台
console = Console()

async def install_browser():
    """
    安装 Chromium 浏览器
    """
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
        console.print("[bold green]浏览器安装完成，请重新运行程序。[/bold green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]发生未知错误：{e}[/bold red]")


async def get_browser():
    """
    启动浏览器实例
    :param headless: 是否以无头模式运行
    :param executable_path: 浏览器可执行文件路径（可选）
    :return: 浏览器实例
    """

    headless = True

    env = get_environment()
    if env == Environment.LOCAL:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
            os.path.join(os.path.dirname(__file__), PLAYWRIGHT_BROWSERS_PATH)
        )
        if DEBUG:
            headless = False
    elif env == Environment.PACKED:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
            os.path.join(os.path.dirname(sys.executable), PLAYWRIGHT_BROWSERS_PATH)
        )

    try:
        # 启动浏览器
        playwright = await async_playwright().start()
        if GUI:
            headless = False  # 使用GUI参数强制非无头模式
        
        browser = await playwright.chromium.launch(headless=headless)
        return playwright, browser
    except Exception as e:
        # 捕获浏览器启动错误
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            console.print("[bold red]浏览器可执行文件不存在！[/bold red]")
            await install_browser()
            sys.exit(1)
        else:
            traceback.print_exc()
