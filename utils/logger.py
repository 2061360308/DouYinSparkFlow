import logging
import os
from logging.handlers import RotatingFileHandler

# 容器可把日志定向到只读应用目录之外的临时挂载。
LOG_DIR = os.environ.get("SPARK_LOG_DIR", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# 日志格式
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"

# 日志文件路径
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# 配置日志
def setup_logger(name="app", level=logging.INFO):
    """
    配置日志记录器
    :param name: 日志记录器名称
    :param level: 日志级别
    :return: 配置好的日志记录器
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 防止重复添加处理器
    if not logger.handlers:
        # 控制台日志处理器
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_formatter = logging.Formatter(LOG_FORMAT)
        console_handler.setFormatter(console_formatter)

        # 文件日志处理器（带日志轮转）
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setLevel(level)
        file_formatter = logging.Formatter(LOG_FORMAT)
        file_handler.setFormatter(file_formatter)

        # 添加处理器到日志记录器
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


# 示例：使用日志记录器
if __name__ == "__main__":
    logger = setup_logger(level=logging.DEBUG)
    logger.debug("这是一个调试信息")
    logger.info("这是一个普通信息")
    logger.warning("这是一个警告信息")
    logger.error("这是一个错误信息")
    logger.critical("这是一个严重错误信息")
