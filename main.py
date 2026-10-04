"""程序入口：按启动模式分派。

    python main.py [task]          跑一轮任务（默认；GitHub Actions / Docker cron）
    python main.py fc              云函数模式：起 HTTP Server 等定时触发器
    python main.py app             本地可视化工具（配置生成 / 账户登录 / 定时任务）
    python main.py scheduler ...   注册/卸载本机系统定时任务（见 app/scheduler/）

命令行参数优先于环境变量 RUN_MODE；都不指定时默认 task。
PyInstaller 打包的 exe（sys.frozen）不带参数时默认 app。
"""

import os
import sys


def _load_env() -> None:
    """加载 .env：优先当前目录，其次工具数据目录（deb 装在 /opt 时）。"""
    if os.path.exists(".env"):
        from dotenv import load_dotenv

        load_dotenv(".env")
        return
    try:
        from app import paths
    except Exception:
        return
    if paths.ENV_FILE.is_file():
        from dotenv import load_dotenv

        load_dotenv(paths.ENV_FILE)


_load_env()

# Windows 的 C 运行时读不懂 IANA 时区名（如 Asia/Shanghai），.env 里的 TZ 会把
# datetime.now()/date.today() 带偏（实测差 7 小时）；Windows 直接用系统时区，删掉 TZ。
# Linux/Docker 保留 TZ（cron 需要）。
if sys.platform == "win32":
    os.environ.pop("TZ", None)

_DEFAULT_MODE = "app" if getattr(sys, "frozen", False) else "task"
MODE = (
    sys.argv[1] if len(sys.argv) > 1 else os.getenv("RUN_MODE", _DEFAULT_MODE)
).strip().lower()


def main():
    if MODE in {"fc", "serve"}:
        from core.fc_server import serve

        serve()
    elif MODE in {"task", "run", "cli", ""}:
        from core.tasks import runTasks

        raise SystemExit(runTasks())
    elif MODE in {"scheduler", "schedule"}:
        from app.scheduler.cli import main as scheduler_main

        raise SystemExit(scheduler_main(sys.argv[2:]))
    elif MODE in {"app", "configtool", "config", "gui", "tool"}:
        from app.web.host import run as app_run

        raise SystemExit(app_run())
    else:
        print(f"未知启动模式: {MODE}（可选：task / fc / app / scheduler）", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()