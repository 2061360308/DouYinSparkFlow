"""本机定时任务子模块（app 的一部分）。

把「跑一轮任务」注册成操作系统的计划任务，支持三种模式：

    常驻定时 (scheduled)  每天固定时间执行（Windows schtasks DAILY / Linux cron）
    开机执行 (boot)       开机或登录时检查，当天没成功跑过就补跑
    生成配置 (config)     不注册任务，只产 .env 供别处（Docker 等）使用

命令按当前运行形态推导：源码运行用 ``python main.py task``，打包成 exe 后用
``<exe> task`` —— 不写死解释器路径。
"""

from __future__ import annotations

from app.scheduler.api import (
    MODE_BOOT,
    MODE_CONFIG,
    MODE_LABELS,
    MODE_SCHEDULED,
    MODES,
    ensure_default_mode,
    get_status,
    run_if_due,
    set_mode,
    uninstall,
)

__all__ = [
    "MODE_SCHEDULED",
    "MODE_BOOT",
    "MODE_CONFIG",
    "MODES",
    "MODE_LABELS",
    "get_status",
    "set_mode",
    "uninstall",
    "run_if_due",
    "ensure_default_mode",
]
