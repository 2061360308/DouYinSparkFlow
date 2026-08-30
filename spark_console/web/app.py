from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Engine

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import (
    DouyinAccount,
    DouyinContactIdentity,
    DouyinConversation,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
    WorkerLock,
)
from spark_console.rate_limit import FailedAttemptLimiter
from spark_console.security import PasswordService, SessionService
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services import Conflict, NotFound, ValidationError
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

ADMIN_QUERY_KEYS = (
    "invite_page",
    "invite_status",
    "task_page",
    "task_q",
    "task_status",
)
INVITE_STATUS_FILTERS = {
    "active": "有效",
    "used": "已使用",
    "expired": "已过期",
    "revoked": "已撤销",
}
SAFE_MANUAL_RETRY_CODES = {
    "network_unavailable",
    "conversation_not_opened",
    "target_not_found",
}
ADMIN_NOTICES = {
    "task_updated": "任务已保存，下一次执行时间已重新计算",
    "task_toggled": "任务状态已更新",
    "task_deleted": "任务已删除",
    "retry_scheduled": "已安排 1 分钟后安全重试",
    "retry_already_scheduled": "该失败记录已经安排过重试",
    "retry_not_allowed": "该记录可能已提交发送，不能快捷重试",
}


def _aware_utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _positive_page(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


def _page_info(items, requested: int, per_page: int):
    total = len(items)
    pages = max(1, ceil(total / per_page))
    current = min(requested, pages)
    start = (current - 1) * per_page
    return items[start : start + per_page], {
        "page": current,
        "pages": pages,
        "total": total,
        "has_previous": current > 1,
        "has_next": current < pages,
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

    def admin_query(request: Request, **overrides) -> dict[str, str]:
        values = {
            key: request.query_params.get(key, "")[:80]
            for key in ADMIN_QUERY_KEYS
            if request.query_params.get(key)
        }
        for key, value in overrides.items():
            if key not in ADMIN_QUERY_KEYS:
                continue
            if value in (None, "", 1, "1"):
                values.pop(key, None)
            else:
                values[key] = str(value)[:80]
        return values

    def admin_url(request: Request, notice: str | None = None, **overrides) -> str:
        values = admin_query(request, **overrides)
        if notice in ADMIN_NOTICES:
            values["notice"] = notice
        query = urlencode(values)
        return f"/admin?{query}" if query else "/admin"

    def admin_dashboard_context(request: Request, db):
        now = datetime.now(timezone.utc)
        users = db.scalars(select(User).order_by(User.created_at)).all()
        task_query = (
            select(SparkTask, User)
            .join(User, SparkTask.owner_user_id == User.id)
            .order_by(SparkTask.send_time, SparkTask.created_at)
        )
        task_q = request.query_params.get("task_q", "").strip()[:80]
        task_status = request.query_params.get("task_status", "all")
        if task_q:
            pattern = f"%{task_q.lower()}%"
            task_query = task_query.where(
                or_(
                    func.lower(User.username).like(pattern),
                    func.lower(SparkTask.target_name).like(pattern),
                )
            )
        if task_status == "enabled":
            task_query = task_query.where(SparkTask.enabled.is_(True))
        elif task_status == "paused":
            task_query = task_query.where(SparkTask.enabled.is_(False))
        else:
            task_status = "all"
        task_rows = list(db.execute(task_query).all())
        tasks, task_page = _page_info(
            task_rows, _positive_page(request.query_params.get("task_page")), 6
        )

        invite_status = request.query_params.get("invite_status", "all")
        invite_items = admin_invite_items(db, cipher)
        if invite_status in INVITE_STATUS_FILTERS:
            label = INVITE_STATUS_FILTERS[invite_status]
            invite_items = [item for item in invite_items if item.status == label]
        else:
            invite_status = "all"
        invites, invite_page = _page_info(
            invite_items, _positive_page(request.query_params.get("invite_page")), 5
        )

        runs = list(
            db.execute(
                select(TaskRun, SparkTask, User)
                .join(SparkTask, TaskRun.task_id == SparkTask.id)
                .join(User, SparkTask.owner_user_id == User.id)
                .order_by(TaskRun.scheduled_for.desc())
                .limit(20)
            ).all()
        )
        retryable_run_ids = set()
        latest_run_by_task = {}
        failures_by_task = {}
        closed_failure_streaks = set()
        for run, task, _owner in runs:
            latest_run_by_task.setdefault(task.id, run.id)
            if task.id in closed_failure_streaks:
                continue
            if run.status == "failed":
                failures_by_task[task.id] = failures_by_task.get(task.id, 0) + 1
            else:
                failures_by_task.setdefault(task.id, 0)
                closed_failure_streaks.add(task.id)
        for run, task, _owner in runs:
            if (
                run.status == "failed"
                and run.error_code in SAFE_MANUAL_RETRY_CODES
                and task.enabled
                and latest_run_by_task.get(task.id) == run.id
            ):
                retryable_run_ids.add(run.id)

        lock = db.get(WorkerLock, 1)
        worker_online = bool(
            lock and _aware_utc(lock.lease_until) and _aware_utc(lock.lease_until) > now
        )
        invalid_accounts = db.scalar(
            select(func.count(DouyinAccount.id)).where(
                DouyinAccount.validation_state == "invalid"
            )
        ) or 0
        repeated_failure_tasks = sum(
            1 for count in failures_by_task.values() if count >= 3
        )
        query_values = admin_query(request)
        query_suffix = f"?{urlencode(query_values)}" if query_values else ""
        notice = request.query_params.get("notice", "")
        return {
            "users": users,
            "tasks": tasks,
            "runs": runs,
            "invites": invites,
            "users_total": len(users),
            "tasks_total": db.scalar(select(func.count(SparkTask.id))) or 0,
            "runs_total": db.scalar(select(func.count(TaskRun.id))) or 0,
            "task_page": task_page,
            "invite_page": invite_page,
            "task_q": task_q,
            "task_status": task_status,
            "invite_status": invite_status,
            "admin_url": lambda **overrides: admin_url(request, **overrides),
            "admin_query_suffix": query_suffix,
            "worker_online": worker_online,
            "worker_id": lock.worker_id if worker_online else None,
            "invalid_accounts": invalid_accounts,
            "repeated_failure_tasks": repeated_failure_tasks,
            "retryable_run_ids": retryable_run_ids,
            "updated_at": now,
            "notice_message": ADMIN_NOTICES.get(notice),
        }

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
            conversation_names = db.scalars(
                select(DouyinConversation.display_name)
                .where(DouyinConversation.account_id == account_id)
                .order_by(DouyinConversation.display_name)
            ).all()
            contacts = db.scalars(
                select(DouyinContactIdentity)
                .where(DouyinContactIdentity.account_id == account_id)
            ).all()
            aliases = {
                value
                for contact in contacts
                for value in (
                    contact.remark_name,
                    contact.nickname,
                    contact.unique_id,
                    contact.short_id,
                )
                if value
            }
            items = [
                {
                    "name": contact.remark_name
                    or contact.nickname
                    or contact.unique_id
                    or contact.short_id,
                    "sec_uid": contact.sec_uid,
                }
                for contact in contacts
                if contact.remark_name
                or contact.nickname
                or contact.unique_id
                or contact.short_id
            ]
            items.extend(
                {"name": name, "sec_uid": None}
                for name in conversation_names
                if name not in aliases
            )
            return {"items": sorted(items, key=lambda item: item["name"])}

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
        target_sec_uid: str = Form(default=""),
        send_time: str = Form(), message_template: str = Form(),
    ):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            service = TaskService(db, AccountService(db, cipher, AuditService(db)), AuditService(db))
            service.create(
                user.id,
                account_id,
                target_name,
                send_time,
                message_template,
                target_sec_uid=target_sec_uid,
            )
        return RedirectResponse("/tasks", status_code=303)

    @app.get("/tasks/{task_id}/edit")
    def edit_task_page(request: Request, task_id: str):
        with session_scope(engine) as db:
            user, _record, context = auth.user_context(request, db)
            account_service = AccountService(db, cipher, AuditService(db))
            service = TaskService(db, account_service, AuditService(db))
            try:
                task = service.get_owned(user.id, task_id)
            except NotFound as error:
                raise HTTPException(404) from error
            binding = db.get(SparkTaskTargetIdentity, task.id)
            form = {
                "account_id": task.douyin_account_id or "",
                "target_name": task.target_name,
                "target_sec_uid": binding.sec_uid if binding else "",
                "send_time": task.send_time,
                "message_template": task.message_template,
            }
            return page(
                request,
                "task_edit.html",
                title="编辑续火任务",
                task=task,
                accounts=account_service.list_owned(user.id),
                form=form,
                error=None,
                **context,
            )

    @app.post("/tasks/{task_id}/edit")
    def edit_task(
        request: Request,
        task_id: str,
        csrf_token: str = Form(default=""),
        account_id: str = Form(),
        target_name: str = Form(),
        target_sec_uid: str = Form(default=""),
        send_time: str = Form(),
        message_template: str = Form(),
    ):
        with session_scope(engine) as db:
            user, record, context = auth.user_context(request, db)
            auth.csrf(record, csrf_token)
            account_service = AccountService(db, cipher, AuditService(db))
            service = TaskService(db, account_service, AuditService(db))
            try:
                task = service.get_owned(user.id, task_id)
                service.update_owned(
                    user.id,
                    task_id,
                    account_id,
                    target_name,
                    send_time,
                    message_template,
                    target_sec_uid=target_sec_uid,
                )
            except NotFound as error:
                raise HTTPException(404) from error
            except (ValidationError, Conflict) as error:
                form = {
                    "account_id": account_id,
                    "target_name": target_name,
                    "target_sec_uid": target_sec_uid,
                    "send_time": send_time,
                    "message_template": message_template,
                }
                return page(
                    request,
                    "task_edit.html",
                    status_code=400,
                    title="编辑续火任务",
                    task=task,
                    accounts=account_service.list_owned(user.id),
                    form=form,
                    error=str(error),
                    **context,
                )
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
            return page(
                request,
                "admin.html",
                title="管理后台",
                **admin_dashboard_context(request, db),
                **context,
            )

    @app.post("/admin/users")
    def admin_create_user(request: Request, csrf_token: str = Form(default=""), username: str = Form()):
        with session_scope(engine) as db:
            admin, record, context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            service = UserService(db, passwords, AuditService(db))
            _user, temporary = service.create(username)
            return page(
                request,
                "admin.html",
                title="管理后台",
                one_time_password=temporary,
                **admin_dashboard_context(request, db),
                **context,
            )

    @app.post("/admin/users/{user_id}/toggle")
    def admin_toggle_user(request: Request, user_id: str, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            user = db.get(User, user_id)
            if user is None:
                raise HTTPException(404)
            UserService(db, passwords, AuditService(db)).set_disabled(admin.id, user.id, user.status == "active")
        return RedirectResponse(admin_url(request), status_code=303)

    @app.post("/admin/users/{user_id}/delete")
    def admin_delete_user(request: Request, user_id: str, csrf_token: str = Form(default=""), confirmation: str = Form()):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            UserService(db, passwords, AuditService(db)).delete(admin.id, user_id, confirmation)
        return RedirectResponse(admin_url(request), status_code=303)

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
        return RedirectResponse(admin_url(request, "task_toggled"), status_code=303)

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
        return RedirectResponse(admin_url(request, "task_deleted"), status_code=303)

    @app.get("/admin/accounts/{account_id}/conversations")
    def admin_account_conversations(request: Request, account_id: str):
        with session_scope(engine) as db:
            auth.admin_context(request, db)
            account = db.get(DouyinAccount, account_id)
            if account is None:
                raise HTTPException(404)
            contacts = db.scalars(
                select(DouyinContactIdentity).where(
                    DouyinContactIdentity.account_id == account_id
                )
            ).all()
            items = [
                {
                    "name": contact.remark_name
                    or contact.nickname
                    or contact.unique_id
                    or contact.short_id,
                    "sec_uid": contact.sec_uid,
                }
                for contact in contacts
                if contact.remark_name
                or contact.nickname
                or contact.unique_id
                or contact.short_id
            ]
            return {"items": sorted(items, key=lambda item: item["name"])}

    @app.get("/admin/tasks/{task_id}/edit")
    def admin_edit_task_page(request: Request, task_id: str):
        with session_scope(engine) as db:
            _admin, _record, context = auth.admin_context(request, db)
            task = db.get(SparkTask, task_id)
            if task is None:
                raise HTTPException(404)
            account_service = AccountService(db, cipher, AuditService(db))
            binding = db.get(SparkTaskTargetIdentity, task.id)
            form = {
                "account_id": task.douyin_account_id or "",
                "target_name": task.target_name,
                "target_sec_uid": binding.sec_uid if binding else "",
                "send_time": task.send_time,
                "message_template": task.message_template,
            }
            return page(
                request,
                "task_edit.html",
                title="编辑续火任务",
                task=task,
                accounts=account_service.list_owned(task.owner_user_id),
                form=form,
                error=None,
                back_url=admin_url(request),
                conversation_prefix="/admin/accounts",
                **context,
            )

    @app.post("/admin/tasks/{task_id}/edit")
    def admin_edit_task(
        request: Request,
        task_id: str,
        csrf_token: str = Form(default=""),
        account_id: str = Form(),
        target_name: str = Form(),
        target_sec_uid: str = Form(default=""),
        send_time: str = Form(),
        message_template: str = Form(),
    ):
        with session_scope(engine) as db:
            admin, record, context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            task = db.get(SparkTask, task_id)
            if task is None:
                raise HTTPException(404)
            account_service = AccountService(db, cipher, AuditService(db))
            service = TaskService(db, account_service, AuditService(db))
            try:
                service.update_owned(
                    task.owner_user_id,
                    task.id,
                    account_id,
                    target_name,
                    send_time,
                    message_template,
                    target_sec_uid=target_sec_uid,
                )
                AuditService(db).write(
                    admin.id, "admin.task.updated", "spark_task", task.id
                )
            except (ValidationError, Conflict) as error:
                form = {
                    "account_id": account_id,
                    "target_name": target_name,
                    "target_sec_uid": target_sec_uid,
                    "send_time": send_time,
                    "message_template": message_template,
                }
                return page(
                    request,
                    "task_edit.html",
                    status_code=400,
                    title="编辑续火任务",
                    task=task,
                    accounts=account_service.list_owned(task.owner_user_id),
                    form=form,
                    error=str(error),
                    back_url=admin_url(request),
                    conversation_prefix="/admin/accounts",
                    **context,
                )
        return RedirectResponse(admin_url(request, "task_updated"), status_code=303)

    @app.post("/admin/runs/{run_id}/retry")
    def admin_retry_run(
        request: Request, run_id: str, csrf_token: str = Form(default="")
    ):
        now = datetime.now(timezone.utc)
        notice = "retry_not_allowed"
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            run = db.get(TaskRun, run_id)
            task = db.get(SparkTask, run.task_id) if run else None
            latest_run_id = (
                db.scalar(
                    select(TaskRun.id)
                    .where(TaskRun.task_id == run.task_id)
                    .order_by(TaskRun.scheduled_for.desc())
                    .limit(1)
                )
                if run
                else None
            )
            if (
                run
                and task
                and task.enabled
                and run.status == "failed"
                and run.error_code in SAFE_MANUAL_RETRY_CODES
                and latest_run_id == run.id
            ):
                current_next = _aware_utc(task.next_run_at)
                already_scheduled = bool(
                    current_next
                    and now < current_next <= now + timedelta(minutes=5)
                )
                if already_scheduled:
                    notice = "retry_already_scheduled"
                else:
                    task.next_run_at = now + timedelta(minutes=1)
                    AuditService(db).write(
                        admin.id,
                        "task.manual_retry_scheduled",
                        "task_run",
                        run.id,
                    )
                    notice = "retry_scheduled"
        return RedirectResponse(admin_url(request, notice), status_code=303)

    return app


def main() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    uvicorn.run(create_app(settings, engine), host=settings.web_bind, port=settings.web_port, proxy_headers=False)


if __name__ == "__main__":
    main()
