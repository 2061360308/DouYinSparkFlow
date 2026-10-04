"""scheduler 命令行入口。

    python -m app.scheduler install --mode scheduled|boot [--time HH:MM] [--exe PATH|--python PATH]
    python -m app.scheduler uninstall
    python -m app.scheduler status
    python -m app.scheduler run-if-due [--force]

app 里通过 API 直接调用，不经过这里；无界面环境（Linux 服务器）用 CLI。
"""

from __future__ import annotations

import argparse
import json
import sys

from app.scheduler import api
from app.scheduler.core import Launcher


def _launcher_from_args(args) -> Launcher:
    if getattr(args, "exe", ""):
        return Launcher.for_exe(args.exe)
    if getattr(args, "python", ""):
        return Launcher.for_python(args.python)
    return Launcher.detect()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scheduler", description="注册本机定时任务")
    sub = parser.add_subparsers(dest="command", required=True)

    install = sub.add_parser("install", help="注册/切换模式")
    install.add_argument("--mode", choices=list(api.MODES), default=api.MODE_SCHEDULED)
    install.add_argument("--time", default="", help="HH:MM（仅常驻定时用；默认取 .env）")
    install.add_argument("--name", default="", help="计划任务名")
    install.add_argument("--exe", default="", help="显式指定打包后的 exe")
    install.add_argument("--python", default="", help="显式指定 python 解释器")

    uninstall = sub.add_parser("uninstall", help="卸载系统任务")
    uninstall.add_argument("--name", default="")

    sub.add_parser("status", help="查看当前模式与注册状态")

    due = sub.add_parser("run-if-due", help="开机补跑（供包装脚本调用）")
    due.add_argument("--force", action="store_true", help="忽略「今天已跑过」强制执行")

    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "install":
            status = api.set_mode(
                args.mode,
                run_time=args.time or None,
                name=args.name or None,
                launcher=_launcher_from_args(args),
            )
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0

        if args.command == "uninstall":
            status = api.uninstall(name=args.name or None)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0

        if args.command == "status":
            print(json.dumps(api.get_status(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "run-if-due":
            result = api.run_if_due(force=args.force)
            print(json.dumps(result, ensure_ascii=False))
            return int(result.get("exit_code", 0))
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
