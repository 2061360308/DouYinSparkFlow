"""程序入口：按启动模式分派。

    python main.py [task]          跑一轮任务（默认；GitHub Actions / Docker cron）
    python main.py fc              云函数模式：起 HTTP Server 等定时触发器
    python main.py configtool      本地配置生成器（configTool）

命令行参数优先于环境变量 RUN_MODE；都不指定时默认 task。
PyInstaller 打包的 exe（sys.frozen）不带参数时默认 configtool。
"""

import os
import sys

if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv(".env")

_DEFAULT_MODE = "configtool" if getattr(sys, "frozen", False) else "task"
MODE = (
    sys.argv[1] if len(sys.argv) > 1 else os.getenv("RUN_MODE", _DEFAULT_MODE)
).strip().lower()


def main():
    if MODE in {"fc", "serve"}:
        from core.fc_server import serve

        serve()
    elif MODE in {"task", "run", "cli", ""}:
        from core.tasks import runTasks

        runTasks()
    elif MODE in {"configtool", "config", "gui", "tool"}:
        from configTool.web.host import run as configtool_run

        raise SystemExit(configtool_run())
    else:
        print(f"未知启动模式: {MODE}（可选：task / fc / configtool）", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()