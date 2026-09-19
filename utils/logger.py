import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"

# 日志落盘位置固定为**仓库根目录**下的 logs/app.log。
# 不能用相对路径 "logs/app.log"：那是相对 CWD 的，从别的目录导入本项目时
# 会在那边凭空建一个 logs/，日志就散了。这里以本文件位置回推仓库根。
LOG_FILE = str(Path(__file__).resolve().parent.parent / "logs" / "app.log")


def resolve_log_level(level):
    if isinstance(level, int):
        return level

    if isinstance(level, str):
        mapping = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }
        return mapping.get(level.lower(), logging.INFO)

    return logging.INFO


def setup_logger(name="app", level="Info"):
    resolved_level = resolve_log_level(level)
    os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(resolved_level)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    if not logger.handlers:
        console_handler = logging.StreamHandler()
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    for handler in logger.handlers:
        handler.setLevel(resolved_level)
        handler.setFormatter(formatter)

    return logger


if __name__ == "__main__":
    logger = setup_logger(level="Debug")
    logger.debug("这是一个调试信息")
    logger.info("这是一个普通信息")
    logger.warning("这是一个警告信息")
    logger.error("这是一个错误信息")
    logger.critical("这是一个严重错误信息")
