from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.engine import Engine

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import (
    DouyinAccount,
    DouyinConversation,
    SparkTask,
    TaskRun,
    User,
)
from spark_console.rate_limit import FailedAttemptLimiter
from spark_console.security import PasswordService, SessionService
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.tasks import TaskService
from spark_console.services.users import UserService
from spark_console.web.account_scan_routes import build_account_scan_router
from spark_console.web.auth import WebAuth
from spark_console.web.registration_routes import admin_invite_items, build_registration_router


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))

RUN_STATUS_LABELS = {
    "running": "执行中",
    "success": "成功",
    "failed": "失败",
    "skipped": "已跳过",
}
RUN_STAGE_LABELS = {
    "starting": "准备执行",
    "authenticating": "验证登录",
    "selecting_target": "查找好友",
    "worker_error": "执行异常",
    "sending": "发送消息",
    "confirming": "确认送达",
    "submitted": "已提交发送",
    "claimed": "已领取",
    "complete": "已完成",
    "missed_startup": "执行器离线",
    "late": "超过发送窗口",
    "clock_check": "时间校验",
    "navigation": "打开聊天页",
    "target_search": "查找好友",
    "message_input": "填写消息",
    "delivery_confirmation": "确认送达",
}


def shanghai_time(value) -> str:
    if value is None:
        return "—"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


templates.env.filters["shanghai_time"] = shanghai_time
templates.env.filters["run_status"] = lambda value: RUN_STATUS_LABELS.get(value, value)
templates.env.filters["run_stage"] = lambda value: RUN_STAGE_LABELS.get(value, value)


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")
    passwords = PasswordService()
    sessions = SessionService(settings.session_key_file.read_bytes())
    auth = WebAuth(sessions)
    registration_limiter = FailedAttemptLimiter()
    cipher = CookieCipher(settings.cookie_key_file.read_bytes())

    def page(request: Request, name: str, status_code: int = 200, **context):
        return templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

    app.include_router(
        build_registration_router(
            engine, passwords, registration_limiter, auth, page, cipher
        )
    )
    app.include_router(build_account_scan_router(engine, auth, cipher))

    @app.exception_handler(401)
    async def unauthorized(request: Request, _exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(409)
    async def change_required(request: Request, exc):
        if exc.detail == "password-change-required":
            return RedirectResponse("/change-password", status_code=303)
        return page(request, "error.html", status_code=409, message="请求冲突")

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return {"status": "ready"}

    @app.get("/")
    def root():
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/login")
    def login_page(request: Request):
        return page(request, "login.html", title="登录", nav=False)

    @app.post("/login")
    def login(request: Request, username: str = Form(), password: str = Form()):
        with session_scope(engine) as db:
            user = db.scalar(select(User).where(User.username == username.strip().lower()))
            valid = user is not None and user.status == "active" and passwords.verify(user.password_hash, password)
            if not valid:
                if user is not None:
                    user.failed_login_count += 1
                return page(
                    request,
                    "login.html",
                    status_code=400,
                    title="登录",
                    nav=False,
                    error="用户名或密码错误",
                )
            user.failed_login_count = 0
            raw, record = sessions.create_record(user.id)
            db.add(record)
            destination = "/change-password" if user.must_change_password else "/dashboard"
        response = RedirectResponse(destination, status_code=303)
        response.set_cookie(
            "spark_session",
            raw,
            httponly=True,
            secure=settings.secure_cookies,
            samesite="strict",
            max_age=8 * 3600,
        )
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = auth.current(request, db, allow_change=True)
            auth.csrf(record, csrf_token)
            db.delete(record)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("spark_session")
        return response

    @app.get("/change-password")
    def change_password_page(request: Request):
        with session_scope(engine) as db:
            user, record = auth.current(request, db, allow_change=True)
            return page(
                request,
                "change_password.html",
                title="设置新密码" if user.must_change_password else "修改密码",
                nav=not user.must_change_password,
                user=user,
                csrf_token=record.csrf_token,
                is_admin=user.role == "admin",
            )

    @app.post("/change-password")
    def change_password(
        request: Request,
        csrf_token: str = Form(default=""),
        current_password: str = Form(),
        new_password: str = Form(),
        new_password_confirmation: str = Form(default=""),
    ):
        with session_scope(engine) as db:
            user, record = auth.current(request, db, allow_change=True)
            auth.csrf(record, csrf_token)
            if not passwords.verify(user.password_hash, current_password):
                return page(
                    request,
                    "change_password.html",
                    400,
                    title="修改密码",
                    nav=not user.must_change_password,
                    user=user,
                    csrf_token=record.csrf_token,
                    is_admin=user.role == "admin",
                    error="当前密码错误",
                )
            if new_password != new_password_confirmation:
                return page(
                    request,
                    "change_password.html",
                    400,
                    title="修改密码",
                    nav=not user.must_change_password,
                    user=user,
                    csrf_token=record.csrf_token,
                    is_admin=user.role == "admin",
                    error="两次输入的新密码不一致",
                )
            try:
                user.password_hash = passwords.hash(new_password)
            except ValueError:
                return page(
                    request,
                    "change_password.html",
                    400,
                    title="修改密码",
                    nav=not user.must_change_password,
                    user=user,
                    csrf_token=record.csrf_token,
                    is_admin=user.role == "admin",
                    error="新密码至少需要 12 位",
                )
            user.must_change_password = False
        return RedirectResponse("/dashboard", status_code=303)

    @app.get("/dashboard")
    def dashboard(request: Request):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            tasks = db.scalars(select(SparkTask).where(SparkTask.owner_user_id == user.id)).all()
            accounts = db.scalars(select(DouyinAccount).where(DouyinAccount.owner_user_id == user.id)).all()
            runs = db.scalars(select(TaskRun).join(SparkTask).where(SparkTask.owner_user_id == user.id).order_by(TaskRun.scheduled_for.desc()).limit(5)).all()
            return page(request, "dashboard.html", title="今日续火", tasks=tasks, accounts=accounts, runs=runs, **context)

    @app.get("/accounts")
    def accounts_page(request: Request):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            accounts = AccountService(db, cipher, AuditService(db)).list_owned(user.id)
            return page(request, "accounts.html", title="抖音账号", accounts=accounts, **context)

    @app.post("/accounts/{account_id}/delete")
    def delete_account(request: Request, account_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            AccountService(db, cipher, AuditService(db)).delete_owned(user.id, account_id)
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/accounts/{account_id}/conversations")
    def account_conversations(request: Request, account_id: str):
        with session_scope(engine) as db:
            user, _record = auth.current(request, db)
            service = AccountService(db, cipher, AuditService(db))
            service.get_owned(user.id, account_id)
            targets = db.scalars(
                select(DouyinConversation.display_name)
                .where(DouyinConversation.account_id == account_id)
                .order_by(DouyinConversation.display_name)
            ).all()
            return {"items": [{"name": target} for target in targets]}

    @app.get("/tasks")
    def tasks_page(request: Request):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            task_service = TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db))
            accounts = AccountService(db, cipher, AuditService(db)).list_owned(user.id)
            return page(request, "tasks.html", title="续火任务", tasks=task_service.list_owned(user.id), accounts=accounts, **context)

    @app.post("/tasks")
    def add_task(
        request: Request,
        csrf_token: str = Form(default=""),
        account_id: str = Form(), target_name: str = Form(),
        send_time: str = Form(), message_template: str = Form(),
    ):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            service = TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db))
            service.create(user.id, account_id, target_name, send_time, message_template)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/toggle")
    def toggle_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            service = TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db))
            task = service.get_owned(user.id, task_id)
            service.set_enabled_owned(user.id, task_id, not task.enabled)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/delete")
    def delete_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db)).delete_owned(user.id, task_id)
        return RedirectResponse("/tasks", status_code=303)

    @app.get("/runs")
    def runs_page(request: Request):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            runs = db.execute(select(TaskRun, SparkTask).join(SparkTask).where(SparkTask.owner_user_id == user.id).order_by(TaskRun.scheduled_for.desc()).limit(100)).all()
            return page(request, "runs.html", title="执行记录", runs=runs, **context)

    @app.get("/admin")
    def admin_page(request: Request):
        with session_scope(engine) as db:
            _admin, _record, context = auth.admin_context(request, db)
            users = db.scalars(select(User).order_by(User.created_at)).all()
            tasks = db.execute(select(SparkTask, User).join(User, SparkTask.owner_user_id == User.id).order_by(SparkTask.send_time)).all()
            runs = db.execute(
                select(TaskRun, SparkTask, User)
                .join(SparkTask, TaskRun.task_id == SparkTask.id)
                .join(User, SparkTask.owner_user_id == User.id)
                .order_by(TaskRun.scheduled_for.desc())
                .limit(20)
            ).all()
            invites = admin_invite_items(db, cipher)
            return page(request, "admin.html", title="管理后台", users=users, tasks=tasks, runs=runs, invites=invites, **context)

    @app.post("/admin/users")
    def admin_create_user(request: Request, csrf_token: str = Form(default=""), username: str = Form()):
        with session_scope(engine) as db:
            admin, record, context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            service = UserService(db, passwords, AuditService(db))
            _user, temporary = service.create(username)
            users = db.scalars(select(User).order_by(User.created_at)).all()
            tasks = db.execute(select(SparkTask, User).join(User, SparkTask.owner_user_id == User.id).order_by(SparkTask.send_time)).all()
            invites = admin_invite_items(db, cipher)
            return page(request, "admin.html", title="管理后台", users=users, tasks=tasks, runs=[], invites=invites, one_time_password=temporary, **context)

    @app.post("/admin/users/{user_id}/toggle")
    def admin_toggle_user(request: Request, user_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            user = db.get(User, user_id)
            if user is None:
                raise HTTPException(404)
            UserService(db, passwords, AuditService(db)).set_disabled(admin.id, user.id, user.status == "active")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/users/{user_id}/delete")
    def admin_delete_user(request: Request, user_id: str, csrf_token: str = Form(default=""), confirmation: str = Form()):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            UserService(db, passwords, AuditService(db)).delete(admin.id, user_id, confirmation)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/tasks/{task_id}/toggle")
    def admin_toggle_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            _admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            task = db.get(SparkTask, task_id)
            if task is None:
                raise HTTPException(404)
            task.enabled = not task.enabled
            AuditService(db).write(_admin.id, "task.enabled" if task.enabled else "task.disabled", "spark_task", task.id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/tasks/{task_id}/delete")
    def admin_delete_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            task = db.get(SparkTask, task_id)
            if task is None:
                raise HTTPException(404)
            db.delete(task)
            AuditService(db).write(admin.id, "task.deleted", "spark_task", task_id)
        return RedirectResponse("/admin", status_code=303)

    return app


def main() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    uvicorn.run(create_app(settings, engine), host=settings.web_bind, port=settings.web_port, proxy_headers=False)


if __name__ == "__main__":
    main()
