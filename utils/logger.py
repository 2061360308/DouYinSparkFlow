import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"


def _log_dir() -> Path:
    """日志目录，必须是可写位置（不能落在打包后的 _internal / /opt 里）。

    1) APP_DATA_DIR：deb/桌面端启动脚本设置的用户数据目录（优先）
    2) 打包运行（sys.frozen）：exe 同级 logs/
    3) 源码运行：仓库根 logs/（以本文件位置回推，避免跟 CWD 走）
    """
    override = os.environ.get("APP_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "logs"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "logs"
    return Path(__file__).resolve().parent.parent / "logs"


def _ensure_writable_dir(path: Path) -> Path:
    """确保日志目录可写；实在不行退回系统临时目录，绝不让启动因写日志而崩。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "douyin-spark-flow" / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


LOG_DIR = _ensure_writable_dir(_log_dir())
LOG_FILE = str(LOG_DIR / "app.log")


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
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    logger = logging.getLogger(name)
    logger.setLevel(resolved_level)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    if not logger.handlers:
        # 打包成 --windowed exe 时没有控制台，sys.stderr 为 None，
        # 此时再加 StreamHandler 会在写日志时报错；只在有 stderr 时才加。
        if sys.stderr is not None:
            logger.addHandler(logging.StreamHandler())
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
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
