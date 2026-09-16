"""统一的路径解析（打包成 exe 后也必须成立）。

本工具要支持两种运行方式：

  1) 开发期：cd configTool && python main.py
  2) 发布期：用 PyInstaller 打包成 exe，双击运行或放到任意目录运行

所以任何路径都不能假设「自己还在项目仓库里」。打包之后 `.venv/`、`loginTool/`、
项目根目录这些全都不存在，凡是从它们推导出来的路径都会失效。

约定：
  - APP_DIR      程序所在目录。可写数据（.env、profiles/）都放这里，跟着程序走。
  - RESOURCE_DIR 只读资源目录。PyInstaller onefile 模式下是临时解包目录，
                 用 sys._MEIPASS 取；源码运行时就等于 APP_DIR。
"""

from __future__ import annotations

import sys
from pathlib import Path

# PyInstaller 打包后会设置 sys.frozen
FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录。

    exe 用自己的文件位置（onefile / onedir 都成立）；
    源码运行时是本文件所在的 configTool 目录。
    """
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """只读资源所在目录。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return app_dir()


APP_DIR = app_dir()

# 用户数据一律写在程序旁边，不依赖项目仓库结构
ENV_FILE = APP_DIR / ".env"
PROFILE_ROOT = APP_DIR / "profiles"
# 抖音号 -> 配置目录名 的对照表，由 profile_store.py 读写
PROFILES_INDEX = APP_DIR / "profiles.json"

# 本工具自己的设置（如抓取隧道），由 local_settings.py 读写。
# 不进 .env：.env 是给主程序（以及云函数）吃的配置文件，工具私有键塞进去会污染它。
LOCAL_SETTINGS = APP_DIR / "local.json"

# 自带的隐身 Chromium 目录名
BROWSER_DIR_NAME = "cloakbrowser-windows-x64"
BROWSER_EXE_NAMES = ("chrome.exe", "chrome")


def browser_dir() -> Path:
    """自带的 Chromium 目录。

    找不到时也返回一个预期路径，方便把「缺浏览器」这件事说清楚。
    """
    candidates = [resource_dir() / BROWSER_DIR_NAME, APP_DIR / BROWSER_DIR_NAME]
    for candidate in candidates:
        if any((candidate / name).is_file() for name in BROWSER_EXE_NAMES):
            return candidate
    return candidates[0]


def browser_binary() -> Path:
    """自带的 Chromium 可执行文件路径。"""
    directory = browser_dir()
    for name in BROWSER_EXE_NAMES:
        if (directory / name).is_file():
            return directory / name
    return directory / BROWSER_EXE_NAMES[0]


# ---------------------------------------------------------------------------
# gost（抓取时接云函数隧道用）
# ---------------------------------------------------------------------------
# Windows 下载包解出来是 gost.exe；源码跑在 Linux/macOS 上时是 gost
GOST_EXE_NAMES = ("gost.exe", "gost")

# 找不到 gost 时给用户的下载页
GOST_RELEASES_URL = "https://github.com/go-gost/gost/releases"


def gost_candidates() -> list:
    """gost 可执行文件的候选路径，按优先级排列。

    用户可以把它放在程序目录、或程序目录下的 gost/、bin/ 子目录；
    打包成 exe 后 resource_dir() 指向解包目录 —— 与自带浏览器同一套查找机制。
    """
    found: list = []
    seen: set = set()
    for root in (resource_dir(), APP_DIR, APP_DIR / "gost", APP_DIR / "bin"):
        for name in GOST_EXE_NAMES:
            candidate = root / name
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                found.append(candidate)
    return found


def gost_binary(explicit: str = "") -> Path:
    """解析 gost 可执行文件路径。

    explicit（界面上填的路径）优先；否则按候选顺序找；都找不到就返回一个预期路径，
    好让报错信息能说清「该把 gost.exe 放哪」。
    """
    text = str(explicit or "").strip()
    if text:
        return Path(text).expanduser()
    found = gost_candidates()
    if found:
        return found[0]
    return APP_DIR / "gost" / GOST_EXE_NAMES[0]
