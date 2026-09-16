"""程序入口 —— 按启动模式分派。

一个镜像要同时支持两种定时部署形态，靠这里分流：

    python main.py        跑一轮任务（= task）。GitHub Actions、服务器 cron 都走这条，
                          不带参数，和以前完全一样。
    python main.py task   同上，显式写法。
    python main.py fc     云函数模式：起 HTTP Server，等定时触发器事件打进来才跑任务。
                          为什么云函数也必须自备 HTTP Server，见 fc_server.py 顶部说明。

也支持环境变量 RUN_MODE 指定模式（命令行参数优先）。
"""

import os
import sys

# 尝试从 .env 文件加载环境变量
if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv(".env")

# 优先级：argv 子命令 > RUN_MODE 环境变量 > 默认 task
MODE = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("RUN_MODE", "task")).strip().lower()


def main():
    if MODE in {"fc", "serve"}:
        from fc_server import serve

        serve()
        return

    if MODE in {"task", "run", "cli", ""}:
        from core.tasks import runTasks

        runTasks()
        return

    print(f"未知启动模式: {MODE}（可选：task / fc）", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
