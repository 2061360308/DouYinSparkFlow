import os, sys
from enum import Enum
import json
import logging
from utils.logger import setup_logger

logger = setup_logger(level=logging.DEBUG)

"""
是否启用调试模式
更详细的日志打印，浏览器操作可视化等
"""
DEBUG = True if os.environ.get("DEBUG", "").lower() == "true" else False
config = None
userData = None


def get_config():
    """
    获取配置信息
    :return: 配置字典
    """
    global config

    if config:
        return config

    config = {
        "proxyAddress": os.getenv("PROXY_ADDRESS", ""),
        "messageTemplate": os.getenv(
            "MESSAGE_TEMPLATE",
            "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]",
        ),
        "hitokotoTypes": json.loads(
            os.getenv("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]')
        ),
        # .env 里统一用**秒**，出口按消费方的单位给：
        #   browserActionTimeout / friendListSettleMs 带单位后缀 → 已是毫秒，调用方直接用
        #   imScanTimeout / imReadyTimeout / imMaxSteps 原本就是秒/步
        "browserActionTimeout": int(
            float(os.getenv("BROWSER_ACTION_TIMEOUT", "120")) * 1000
        ),  # 单次浏览器操作/导航超时（Playwright 级），毫秒
        "imScanTimeout": int(
            os.getenv("IM_SCAN_TIMEOUT", "120")
        ),  # 扫描总预算，秒
        "imReadyTimeout": int(
            os.getenv("IM_READY_TIMEOUT", "120")
        ),  # 门禁等待上限，秒
        "friendListSettleMs": int(
            float(os.getenv("FRIEND_LIST_WAIT_TIME", "3")) * 1000
        ),  # 资料静默窗，毫秒
        "imMaxSteps": int(
            os.getenv("IM_MAX_STEPS", "200")
        ),  # 滚动步数硬上限
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": os.getenv("LOG_LEVEL", "Debug"),  # 日志级别
    }

    return config


def sanitize_cookies(cookies):
    for cookie in cookies:
        if "sameSite" in cookie:
            cookie.pop("sameSite")  # 移除 sameSite 字段，Playwright 可能不支持该字段
    return cookies


def get_userData():
    """
    获取用户数据目录
    :return: 用户数据目录路径
    """
    global userData

    if userData:
        return userData

    tasks = json.loads(os.getenv("TASKS", "[]"))
    
    if DEBUG:
        logger.info(f"读取到tasks：{tasks}")

    userData = []

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        fingerprint = task.get("fingerprint")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = (
            os.getenv(cookies_key, "").encode("utf-8").decode("unicode_escape")
        )
        if not cookies_str:
            logger.warning(f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过")
            continue
        try:
            cookies = json.loads(cookies_str)
        except json.JSONDecodeError:
            logger.warning(f"{username} 的任务 {cookies_key} 格式不正确，已跳过")
            continue

        userData.append(
            {
                "unique_id": unique_id,
                "username": username,
                "fingerprint": fingerprint,
                "cookies": sanitize_cookies(cookies),
                # 目标列表保持原样（不在这里归一化）。
                # 归一化统一由 tasks.py 在匹配前做 —— 读取端不做加工，
                # 配置读出来什么就是什么，避免同一份数据两处变换。
                "targets": list(task.get("targets", [])),
            }
        )

    return userData
