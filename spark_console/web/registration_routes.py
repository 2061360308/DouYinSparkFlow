from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError

from spark_console.db import session_scope
from spark_console.rate_limit import FailedAttemptLimiter
from spark_console.security import PasswordService
from spark_console.services import Conflict, ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.services.users import UserService, validate_registration_password
from spark_console.web.auth import WebAuth


PUBLIC_ERROR = "注册信息或邀请码无效"


def registration_client_key(request: Request) -> str:
    supplied = request.headers.get("x-real-ip")
    if supplied:
        try:
            return str(ipaddress.ip_address(supplied))
        except ValueError:
            pass
    return request.client.host if request.client is not None else "unknown"


def build_registration_router(engine, passwords: PasswordService, limiter: FailedAttemptLimiter, auth: WebAuth, page) -> APIRouter:
    router = APIRouter()

    @router.get("/register")
    def register_page(request: Request):
        return page(request, "register.html", title="注册", nav=False)

    @router.post("/register")
    def register(
        request: Request,
        username: str = Form(),
        password: str = Form(),
        password_confirmation: str = Form(),
        invite_code: str = Form(),
    ):
        key = registration_client_key(request)
        if not limiter.allow(key):
            return page(request, "register.html", 429, title="注册", nav=False, error=PUBLIC_ERROR)
        try:
            with session_scope(engine) as db:
                validate_registration_password(password)
                if password != password_confirmation:
                    raise ValidationError(PUBLIC_ERROR)
                user, _ = UserService(db, passwords, AuditService(db)).create(
                    username, password, "user"
                )
                user.must_change_password = False
                InviteService(db, AuditService(db)).consume(invite_code, user.id)
        except (ValidationError, Conflict, IntegrityError, ValueError):
            limiter.record_failure(key)
            return page(request, "register.html", 400, title="注册", nav=False, error=PUBLIC_ERROR)
        limiter.clear(key)
        return RedirectResponse("/login?registered=1", 303)

    @router.post("/admin/invites")
    def create_invite(request: Request, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            admin, record, context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            _invite, plaintext = InviteService(db, AuditService(db)).create(admin.id)
            invites = InviteService(db, AuditService(db)).list_all()
            users, tasks, runs = _admin_page_data(db)
            return page(
                request,
                "admin.html",
                title="管理后台",
                users=users,
                tasks=tasks,
                runs=runs,
                invites=invites,
                one_time_invite=plaintext,
                **context,
            )

    @router.post("/admin/invites/{invite_id}/revoke")
    def revoke_invite(
        request: Request, invite_id: str, csrf_token: str = Form(default="")
    ):
        with session_scope(engine) as db:
            admin, record, _context = auth.admin_context(request, db)
            auth.csrf(record, csrf_token)
            try:
                InviteService(db, AuditService(db)).revoke(admin.id, invite_id)
            except ValidationError as error:
                raise HTTPException(400, str(error)) from error
        return RedirectResponse("/admin", 303)

    return router


def _admin_page_data(db):
    from sqlalchemy import select

    from spark_console.models import SparkTask, TaskRun, User

    users = db.scalars(select(User).order_by(User.created_at)).all()
    tasks = db.execute(
        select(SparkTask, User)
        .join(User, SparkTask.owner_user_id == User.id)
        .order_by(SparkTask.send_time)
    ).all()
    runs = db.scalars(select(TaskRun).order_by(TaskRun.scheduled_for.desc()).limit(20)).all()
    return users, tasks, runs
