"""调度模块的路径解析（源码运行 / exe 打包都成立）。

并入 app 包后，复用 app/paths.py 的两套目录：

    TOOL_DIR       工具自己的数据目录（源码 = app/；打包 = exe 目录）——
                   .scheduler/、local.json、window.json、profiles.json 都放这。
    ROOT           项目根（源码 = 仓库根；打包 = exe 目录）—— main.py 所在，
                   启动器以它为工作目录。
    ENV_FILE       主程序读的配置（.env，源码 = 仓库根）。
"""

from __future__ import annotations

from app import paths as _tool_paths

FROZEN = _tool_paths.FROZEN

# 工具数据目录（.scheduler/ 等）与项目根（main.py 所在）
TOOL_DIR = _tool_paths.APP_DIR
ROOT = _tool_paths.project_root()
ENV_FILE = _tool_paths.ENV_FILE

# 本模块的元数据目录（与 local.json / window.json 同目录）
SCHEDULER_DIR = TOOL_DIR / ".scheduler"
STATE_FILE = SCHEDULER_DIR / "state.json"
INSTALL_FILE = SCHEDULER_DIR / "install.json"
LOCK_FILE = SCHEDULER_DIR / "lock"

# 被系统任务拉起时的日志
LOG_FILE = TOOL_DIR / "logs" / "scheduler.log"

# 计划任务名（Windows schtasks /TN；Linux cron 用它的注释 marker）
DEFAULT_NAME = "DouYinSparkFlow"
