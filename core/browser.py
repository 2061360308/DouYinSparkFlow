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
    """启动浏览器实例。"""
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
        # 关掉指纹噪声注入、显式指定存储配额：两者都是反检测（FingerprintJS /
        # BrowserScan）推荐的开关。cloakbrowser 默认每次随机指纹，噪声反而会被
        # ML 判成「浏览器被篡改」。注意不要加 --enable-unsafe-swiftshader：它会让
        # WebGL 暴露 SwiftShader 软件渲染串，是典型的机器特征（cloakbrowser 内部
        # 也专门屏蔽它）。
        "--fingerprint-noise=false",
        "--fingerprint-storage-quota=2048",
    ]

    # 固定指纹：传了种子就钉死；没传就不加 —— cloakbrowser 默认会自己塞一个
    # 随机种子（--fingerprint=<随机>），这里再补一个空值的 --fingerprint 反而会
    # 把那个随机种子覆盖掉。同一账号天天换指纹在风控眼里等于「不停换设备」，
    # 所以有种子时必须钉死。
    if fingerprint:
        BASE_CHROME_ARGS.append(f"--fingerprint={str(fingerprint)}")

    launch_kwargs = {"headless": headless, "humanize": True, "args": BASE_CHROME_ARGS}

    try:
        # 启动浏览器（cloakbrowser 自带 humanize 拟人化，调用方不要再叠加延迟）
        if not proxyAddress:
            return launch(**launch_kwargs)

        # 走代理时用 geoip 按出口 IP 自动匹配时区/语言，否则 UTC + en-US 本身
        # 就是风控信号。0.5.10 起 geoip 解析失败会直接抛错中断启动，这里降级
        # 重试一次（失败发生在浏览器真正启动之前，重试是安全的）。
        try:
            return launch(proxy=proxyAddress, geoip=True, **launch_kwargs)
        except RuntimeError as exc:
            print(f"GeoIP 自动时区/语言失败，改用本机默认时区继续：{exc}")
            return launch(proxy=proxyAddress, **launch_kwargs)
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            print("浏览器可执行文件不存在！请安装CloakBrowser")
            sys.exit(1)
        else:
            traceback.print_exc()
