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
DEBUG = True
config = None
userData = None


class Environment(Enum):
    GITHUBACTION = "GITHUB_ACTION"  # GitHub Action 运行
    LOCAL = "LOCAL"  # 本地代码运行
    PACKED = "PACKED"  # PyInstaller 打包运行

    def __str__(self):
        return self.value


def get_environment():
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Environment.PACKED
    elif os.getenv("GITHUB_ACTIONS") == "true":
        return Environment.GITHUBACTION
    else:
        return Environment.LOCAL


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
        "messageTemplate": os.getenv("MESSAGE_TEMPLATE", "[盖瑞]今日火花[加一]\\n—— [右边] 每日一言 [左边] ——\\n[API]"),
        "hitokotoTypes": json.loads(
            os.getenv("HITOKOTO_TYPES", '["文学","影视","诗词","哲学"]')
        ),
        "matchMode": os.getenv("MATCH_MODE", "nickname"),  # 是否使用短 ID 进行好友匹配
        "browserTimeout": int(os.getenv("BROWSER_TIMEOUT", "120000")),  # 浏览器操作超时时间，单位毫秒
        "friendListTimeout": int(os.getenv("FRIEND_LIST_WAIT_TIME", "2000")),  # 好友列表加载超时时间，单位毫秒
        "taskRetryTimes": int(os.getenv("TASK_RETRY_TIMES", "3")),  # 任务重试次数
        "logLevel": os.getenv("LOG_LEVEL", "DEBUG"),  # 日志级别
    }

    return config

def sanitize_cookies(cookies):
    """清洗 Cookie-Editor / Chrome 导出的 cookies，使其符合 Playwright add_cookies 约束。"""
    if not cookies:
        return cookies

    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue

        # Cookie-Editor 常导出 no_restriction / unspecified，Playwright 只接受 Strict / Lax / None
        if "sameSite" in cookie:
            cookie.pop("sameSite")

        # Chrome CHIPS：partitionKey 现为 {topLevelSite, hasCrossSiteAncestor} 对象，
        # Playwright add_cookies 要求 partitionKey 为字符串，否则会报
        # "cookies[0].partitionKey: expected string, got object"
        partition_key = cookie.get("partitionKey")
        if isinstance(partition_key, dict):
            site = partition_key.get("topLevelSite") or partition_key.get("sourceOrigin")
            if isinstance(site, str) and site:
                cookie["partitionKey"] = site
            else:
                cookie.pop("partitionKey", None)
        elif partition_key is not None and not isinstance(partition_key, str):
            cookie.pop("partitionKey", None)

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

    userData = []

    for task in tasks:
        username = task.get("username", "未知用户")
        unique_id = task.get("unique_id")
        if not unique_id:
            logger.warning(f"{username} 的任务  缺少 unique_id 字段，已跳过")
            continue
        cookies_key = f"cookies_{unique_id}".upper()
        cookies_str = (
            os.getenv(cookies_key, "").encode("utf-8").decode("unicode_escape")
        )
        if not cookies_str:
            logger.warning(
                f"{username} 的任务 缺少 {cookies_key} 环境变量，已跳过"
            )
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
                "cookies": sanitize_cookies(cookies),
                "targets": task.get("targets", []),
            }
        )

    return userData
