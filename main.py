# 尝试从配置文件加载环境变量（默认 .env，可通过 CONFIG_FILE 环境变量覆盖）
import os
from dotenv import load_dotenv

env_file = os.getenv("CONFIG_FILE", ".env")
if os.path.exists(env_file):
    load_dotenv(env_file)

from core.tasks import runTasks

runTasks()
