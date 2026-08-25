from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

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
from spark_console.models import DouyinAccount, SparkTask, TaskRun, User, WebSession
from spark_console.security import PasswordService, SessionService
from spark_console.services import ServiceError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.tasks import TaskService
from spark_console.services.users import UserService


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def create_app(settings: Settings, engine: Engine) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")
    passwords = PasswordService()
    sessions = SessionService(settings.session_key_file.read_bytes())
    cipher = CookieCipher(settings.cookie_key_file.read_bytes())

    def current(request: Request, db, allow_change: bool = False) -> tuple[User, WebSession]:
        raw = request.cookies.get("spark_session")
        if not raw:
            raise HTTPException(401)
        record = db.scalar(select(WebSession).where(WebSession.token_hash == sessions.token_hash(raw)))
        if record is None or _aware(record.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(401)
        user = db.get(User, record.user_id)
        if user is None or user.status != "active":
            raise HTTPException(401)
        if user.must_change_password and not allow_change:
            raise HTTPException(409, "password-change-required")
        return user, record

    def csrf(record: WebSession, supplied: str) -> None:
        import secrets
        if not supplied or not secrets.compare_digest(record.csrf_token, supplied):
            raise HTTPException(403, "CSRF validation failed")

    def page(request: Request, name: str, status_code: int = 200, **context):
        return templates.TemplateResponse(
            request=request, name=name, context=context, status_code=status_code
        )

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
            user, record = current(request, db, allow_change=True)
            csrf(record, csrf_token)
            db.delete(record)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("spark_session")
        return response

    @app.get("/change-password")
    def change_password_page(request: Request):
        with session_scope(engine) as db:
            user, record = current(request, db, allow_change=True)
            return page(request, "change_password.html", title="设置新密码", nav=False, user=user, csrf_token=record.csrf_token)

    @app.post("/change-password")
    def change_password(
        request: Request,
        csrf_token: str = Form(default=""),
        current_password: str = Form(),
        new_password: str = Form(),
    ):
        with session_scope(engine) as db:
            user, record = current(request, db, allow_change=True)
            csrf(record, csrf_token)
            if not passwords.verify(user.password_hash, current_password):
                raise HTTPException(400, "当前密码错误")
            user.password_hash = passwords.hash(new_password)
            user.must_change_password = False
        return RedirectResponse("/dashboard", status_code=303)

    def user_context(request: Request, db):
        user, record = current(request, db)
        return user, record, {
            "user": user,
            "csrf_token": record.csrf_token,
            "is_admin": user.role == "admin",
        }

    @app.get("/dashboard")
    def dashboard(request: Request):
        with session_scope(engine) as db:
            user, _record, context = user_context(request, db)
            tasks = db.scalars(select(SparkTask).where(SparkTask.owner_user_id == user.id)).all()
            accounts = db.scalars(select(DouyinAccount).where(DouyinAccount.owner_user_id == user.id)).all()
            runs = db.scalars(select(TaskRun).join(SparkTask).where(SparkTask.owner_user_id == user.id).order_by(TaskRun.scheduled_for.desc()).limit(5)).all()
            return page(request, "dashboard.html", title="今日续火", tasks=tasks, accounts=accounts, runs=runs, **context)

    @app.get("/accounts")
    def accounts_page(request: Request):
        with session_scope(engine) as db:
            user, _record, context = user_context(request, db)
            accounts = AccountService(db, cipher, AuditService(db)).list_owned(user.id)
            return page(request, "accounts.html", title="抖音账号", accounts=accounts, **context)

    @app.post("/accounts")
    def add_account(
        request: Request,
        csrf_token: str = Form(default=""),
        display_name: str = Form(),
        cookies: str = Form(),
    ):
        with session_scope(engine) as db:
            user, record = current(request, db)
            csrf(record, csrf_token)
            try:
                AccountService(db, cipher, AuditService(db)).create(user.id, display_name, cookies)
            except ServiceError as error:
                raise HTTPException(400, str(error)) from error
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/accounts/{account_id}/delete")
    def delete_account(request: Request, account_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = current(request, db)
            csrf(record, csrf_token)
            AccountService(db, cipher, AuditService(db)).delete_owned(user.id, account_id)
        return RedirectResponse("/accounts", status_code=303)

    @app.get("/tasks")
    def tasks_page(request: Request):
        with session_scope(engine) as db:
            user, _record, context = user_context(request, db)
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
            user, record = current(request, db)
            csrf(record, csrf_token)
            service = TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db))
            service.create(user.id, account_id, target_name, send_time, message_template)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/toggle")
    def toggle_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = current(request, db)
            csrf(record, csrf_token)
            service = TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db))
            task = service.get_owned(user.id, task_id)
            service.set_enabled_owned(user.id, task_id, not task.enabled)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/tasks/{task_id}/delete")
    def delete_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = current(request, db)
            csrf(record, csrf_token)
            TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db)).delete_owned(user.id, task_id)
        return RedirectResponse("/tasks", status_code=303)

    @app.get("/runs")
    def runs_page(request: Request):
        with session_scope(engine) as db:
            user, _record, context = user_context(request, db)
            runs = db.execute(select(TaskRun, SparkTask).join(SparkTask).where(SparkTask.owner_user_id == user.id).order_by(TaskRun.scheduled_for.desc()).limit(100)).all()
            return page(request, "runs.html", title="执行记录", runs=runs, **context)

    def admin_context(request: Request, db):
        user, record = current(request, db)
        if user.role != "admin":
            raise HTTPException(404)
        return user, record, {"user": user, "csrf_token": record.csrf_token, "is_admin": True}

    @app.get("/admin")
    def admin_page(request: Request):
        with session_scope(engine) as db:
            _admin, _record, context = admin_context(request, db)
            users = db.scalars(select(User).order_by(User.created_at)).all()
            tasks = db.execute(select(SparkTask, User).join(User, SparkTask.owner_user_id == User.id).order_by(SparkTask.send_time)).all()
            runs = db.scalars(select(TaskRun).order_by(TaskRun.scheduled_for.desc()).limit(20)).all()
            return page(request, "admin.html", title="管理后台", users=users, tasks=tasks, runs=runs, **context)

    @app.post("/admin/users")
    def admin_create_user(request: Request, csrf_token: str = Form(default=""), username: str = Form()):
        with session_scope(engine) as db:
            admin, record, context = admin_context(request, db)
            csrf(record, csrf_token)
            service = UserService(db, passwords, AuditService(db))
            _user, temporary = service.create(username)
            users = db.scalars(select(User).order_by(User.created_at)).all()
            tasks = db.execute(select(SparkTask, User).join(User, SparkTask.owner_user_id == User.id).order_by(SparkTask.send_time)).all()
            return page(request, "admin.html", title="管理后台", users=users, tasks=tasks, runs=[], one_time_password=temporary, **context)

    @app.post("/admin/users/{user_id}/toggle")
    def admin_toggle_user(request: Request, user_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, _context = admin_context(request, db)
            csrf(record, csrf_token)
            user = db.get(User, user_id)
            if user is None:
                raise HTTPException(404)
            UserService(db, passwords, AuditService(db)).set_disabled(admin.id, user.id, user.status == "active")
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/users/{user_id}/delete")
    def admin_delete_user(request: Request, user_id: str, csrf_token: str = Form(default=""), confirmation: str = Form()):
        with session_scope(engine) as db:
            admin, record, _context = admin_context(request, db)
            csrf(record, csrf_token)
            UserService(db, passwords, AuditService(db)).delete(admin.id, user_id, confirmation)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/tasks/{task_id}/toggle")
    def admin_toggle_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            _admin, record, _context = admin_context(request, db)
            csrf(record, csrf_token)
            task = db.get(SparkTask, task_id)
            if task is None:
                raise HTTPException(404)
            task.enabled = not task.enabled
            AuditService(db).write(_admin.id, "task.enabled" if task.enabled else "task.disabled", "spark_task", task.id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/tasks/{task_id}/delete")
    def admin_delete_task(request: Request, task_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, _context = admin_context(request, db)
            csrf(record, csrf_token)
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
