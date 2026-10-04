"""统一的路径解析（打包成 exe 后也必须成立）。

本工具要支持两种运行方式：

  1) 开发期：在仓库根执行 `python main.py app`
  2) 发布期：用 PyInstaller 打包成 exe，双击运行或放到任意目录运行

所以任何路径都不能假设「自己还在项目仓库里」。打包之后 `.venv/`、
项目根目录这些全都不存在，凡是从它们推导出来的路径都会失效。

约定：
  - APP_DIR      程序所在目录。工具自己的数据（profiles/、profiles.json、
                 local.json）都放这里，跟着程序走。
  - RESOURCE_DIR 只读资源目录。PyInstaller onefile 模式下是临时解包目录，
                 用 sys._MEIPASS 取；源码运行时就等于 APP_DIR。
  - ROOT_DIR     项目根。只有 .env 放这里 —— 见 project_root()。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import util

# PyInstaller 打包后会设置 sys.frozen
FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录。

    exe 用自己的文件位置（onefile / onedir 都成立）；
    源码运行时是本文件所在的 app 目录。
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


def project_root() -> Path:
    """写 .env 的目录（项目根）。

    .env 是供主程序读取的配置，要直接生成在它读的位置上：源码运行 = 仓库根
    （app/ 的上一层），打包运行 = exe 所在目录。父目录不像项目根时
    退回程序目录，避免把配置写到别处。
    """
    if FROZEN:
        return APP_DIR
    parent = APP_DIR.parent
    if (parent / "main.py").is_file() or (parent / "core").is_dir():
        return parent
    return APP_DIR


ROOT_DIR = project_root()

# 主程序读的那份配置（见 project_root 的说明）
ENV_FILE = ROOT_DIR / ".env"

# 以下都是本工具自己的元数据，一律写在程序旁边，不依赖项目仓库结构
PROFILE_ROOT = APP_DIR / "profiles"
# 抖音号 -> 配置目录名 的对照表，由 profile_store.py 读写
PROFILES_INDEX = APP_DIR / "profiles.json"

# 本工具自己的设置（如抓取隧道），由 settings.py 读写。
# 不进 .env：.env 是供主程序（以及云函数）读取的配置文件，工具私有键放进去会污染它。
LOCAL_SETTINGS = APP_DIR / "local.json"

# 本机定时任务（app/scheduler）的数据目录：状态、安装记录、启动脚本、日志
SCHEDULER_DIR = APP_DIR / ".scheduler"
SCHEDULER_STATE = SCHEDULER_DIR / "state.json"
SCHEDULER_INSTALL = SCHEDULER_DIR / "install.json"
SCHEDULER_LOCK = SCHEDULER_DIR / "lock"
SCHEDULER_LOG = APP_DIR / "logs" / "scheduler.log"

# 自带的隐身 Chromium 目录名
BROWSER_DIR_NAME = "cloakbrowser-windows-x64"
BROWSER_EXE_NAMES = ("chrome.exe", "chrome")


def browser_dir() -> Path:
    """自带的 Chromium 目录。

    找不到时也返回一个预期路径，方便把「缺浏览器」这件事说清楚。
    """
    candidates = [resource_dir() / BROWSER_DIR_NAME, APP_DIR / BROWSER_DIR_NAME]
    for candidate in candidates:
        if util.find_binary(BROWSER_EXE_NAMES, [candidate]):
            return candidate
    return candidates[0]


def browser_binary() -> Path:
    """自带的 Chromium 可执行文件路径。"""
    directory = browser_dir()
    found = util.find_binary(BROWSER_EXE_NAMES, [directory])
    return found if found else directory / BROWSER_EXE_NAMES[0]


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
