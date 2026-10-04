"""跨模块的小工具：时间戳、可执行文件查找、用系统默认程序打开。"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def now_str() -> str:
    """当前本地时间，格式 YYYY-MM-DD HH:MM:SS。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def find_binary(names, roots):
    """在若干目录里按顺序找第一个存在的文件；找不到返回 None。

    browser/gost 的可执行文件查找共用（Windows 下 .exe / 无扩展名两种名字）。
    """
    for root in roots:
        for name in names:
            candidate = Path(root) / name
            if candidate.is_file():
                return candidate
    return None


def open_in_system(target) -> str:
    """用系统默认程序打开路径或 URL（Windows/macOS/Linux 通用），返回其文本形式。"""
    text = str(target)
    try:
        if os.name == "nt":
            os.startfile(text)  # noqa: S606 —— Windows 上交给 shell 关联打开
        elif sys.platform == "darwin":
            subprocess.Popen(["open", text])
        else:
            subprocess.Popen(["xdg-open", text])
    except Exception as exc:
        raise RuntimeError(f"打开失败：{type(exc).__name__}: {exc}") from exc
    return text
