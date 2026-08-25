from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import User
from spark_console.security import PasswordService
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.tasks import TaskService
from spark_console.services.users import UserService


def runtime():
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    return settings, engine


def command_create_admin(args) -> int:
    _settings, engine = runtime()
    password = getpass.getpass("管理员密码: ")
    confirmation = getpass.getpass("再次输入: ")
    if password != confirmation:
        raise SystemExit("两次密码不一致")
    with session_scope(engine) as db:
        UserService(db, PasswordService(), AuditService(db)).create(args.username, password, "admin")
    print("管理员已创建；首次登录必须修改密码。")
    return 0


def command_create_user(args) -> int:
    _settings, engine = runtime()
    with session_scope(engine) as db:
        _user, temporary = UserService(db, PasswordService(), AuditService(db)).create(args.username)
    print(f"临时密码（仅显示一次）: {temporary}")
    return 0


def command_import_legacy(args) -> int:
    settings, engine = runtime()
    payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
    accounts_created = tasks_created = 0
    with session_scope(engine) as db:
        owner = db.scalar(select(User).where(User.username == args.owner.lower()))
        if owner is None:
            raise SystemExit("指定用户不存在")
        audit = AuditService(db)
        accounts = AccountService(db, CookieCipher(settings.cookie_key_file.read_bytes()), audit)
        tasks = TaskService(db, accounts, audit)
        for index, item in enumerate(payload, 1):
            account = accounts.create(owner.id, item.get("username") or f"导入账号{index}", json.dumps(item["cookies"], ensure_ascii=False))
            accounts_created += 1
            for target in item.get("targets", []):
                tasks.create(owner.id, account.id, target, args.time, args.message)
                tasks_created += 1
    print(f"导入完成：账号 {accounts_created} 个，任务 {tasks_created} 个。")
    return 0


def command_backup(_args) -> int:
    settings, _engine = runtime()
    source = settings.data_dir / "spark.db"
    destination = settings.data_dir / "backups" / f"spark-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    shutil.copy2(source, destination)
    os.chmod(destination, 0o600)
    print(destination)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spark-console")
    commands = parser.add_subparsers(required=True)
    admin = commands.add_parser("create-admin"); admin.add_argument("username"); admin.set_defaults(handler=command_create_admin)
    user = commands.add_parser("create-user"); user.add_argument("username"); user.set_defaults(handler=command_create_user)
    legacy = commands.add_parser("import-legacy"); legacy.add_argument("path"); legacy.add_argument("--owner", required=True); legacy.add_argument("--time", default="09:00"); legacy.add_argument("--message", default="今日火花"); legacy.set_defaults(handler=command_import_legacy)
    backup = commands.add_parser("backup-db"); backup.set_defaults(handler=command_backup)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
