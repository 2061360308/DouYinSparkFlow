from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from threading import Lock

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from spark_console.crypto import CookieCipher
from spark_console.db import session_scope
from spark_console.models import utc_now
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.scan_sessions import ScanSessionService
from spark_console.web.auth import WebAuth


NO_STORE = {"Cache-Control": "no-store"}
START_LIMIT = 5
STATUS_LIMIT = 40
QR_LIMIT = 40
CANCEL_LIMIT = 10
INTERACT_LIMIT = 120


class ScanRequestLimiter:
    """Small per-process limiter for authenticated scan API traffic."""

    def __init__(self, window: timedelta = timedelta(minutes=1), now=utc_now):
        self.window = window
        self.now = now
        self._requests: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def allow(self, key: str, limit: int) -> bool:
        with self._lock:
            values = self._requests[key]
            current = self.now()
            cutoff = current - self.window
            while values and values[0] <= cutoff:
                values.popleft()
            if len(values) >= limit:
                return False
            values.append(current)
            return True


def _error(status_code: int, error: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": error, "message": message},
        status_code=status_code,
        headers=NO_STORE,
    )


def build_account_scan_router(
    engine,
    auth: WebAuth,
    cipher: CookieCipher,
    limiter: ScanRequestLimiter | None = None,
) -> APIRouter:
    router = APIRouter()
    request_limiter = limiter or ScanRequestLimiter()

    def limited(user_id: str, action: str, limit: int) -> JSONResponse | None:
        if request_limiter.allow(f"{user_id}:{action}", limit):
            return None
        return _error(429, "rate_limited", "请求过于频繁，请稍后重试")

    @router.post("/accounts/scan")
    def start_scan(request: Request, csrf_token: str = Form(default="")):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            if response := limited(user.id, "start", START_LIMIT):
                return response
            service = ScanSessionService(db)
            service.expire_stale()
            try:
                scan = service.start(user.id)
            except Conflict:
                scan = service.active_owned(user.id)
                if scan is None:
                    return _error(409, "slot_busy", "扫码通道正在使用，请稍后重试")
                projection = service.public_status(scan)
                return JSONResponse(projection, status_code=200, headers=NO_STORE)
            projection = service.public_status(scan)
        return JSONResponse(projection, status_code=201, headers=NO_STORE)

    @router.get("/accounts/scan/{scan_id}")
    def scan_status(request: Request, scan_id: str):
        with session_scope(engine) as db:
            user, _record = auth.current(request, db)
            if response := limited(user.id, "status", STATUS_LIMIT):
                return response
            service = ScanSessionService(db)
            service.expire_stale()
            try:
                scan = service.get_owned(user.id, scan_id)
            except NotFound:
                return _error(404, "not_found", "未找到扫码会话")
            projection = service.public_status(scan)
        return JSONResponse(projection, headers=NO_STORE)

    @router.get("/accounts/scan/{scan_id}/qr")
    def scan_qr(request: Request, scan_id: str):
        with session_scope(engine) as db:
            user, _record = auth.current(request, db)
            if response := limited(user.id, "qr", QR_LIMIT):
                return response
            service = ScanSessionService(db)
            service.expire_stale()
            try:
                scan = service.get_owned(user.id, scan_id)
            except NotFound:
                return _error(404, "not_found", "未找到扫码会话")
            png = scan.qr_png
            if png is None:
                return _error(404, "qr_unavailable", "二维码尚未就绪")
        return Response(content=png, media_type="image/png", headers=NO_STORE)

    @router.post("/accounts/scan/{scan_id}/cancel")
    def cancel_scan(
        request: Request, scan_id: str, csrf_token: str = Form(default="")
    ):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            if response := limited(user.id, "cancel", CANCEL_LIMIT):
                return response
            service = ScanSessionService(db)
            service.expire_stale()
            try:
                scan = service.cancel_owned(user.id, scan_id)
            except NotFound:
                return _error(404, "not_found", "未找到扫码会话")
            except Conflict:
                return _error(409, "scan_not_active", "扫码会话已结束")
            projection = service.public_status(scan)
        return JSONResponse(projection, headers=NO_STORE)

    @router.post("/accounts/scan/{scan_id}/interact")
    def interact_scan(
        request: Request,
        scan_id: str,
        csrf_token: str = Form(default=""),
        x: float = Form(default=-1),
        y: float = Form(default=-1),
    ):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            if response := limited(user.id, "interact", INTERACT_LIMIT):
                return response
            try:
                ScanSessionService(db).queue_click(user.id, scan_id, x, y)
            except NotFound:
                return _error(404, "not_found", "未找到扫码会话")
            except ValidationError:
                return _error(400, "invalid_interaction", "点击位置无效")
            except Conflict:
                return _error(409, "scan_not_active", "扫码会话已结束")
        return JSONResponse({"accepted": True}, status_code=202, headers=NO_STORE)

    @router.post("/accounts/{account_id}/rename")
    def rename_account(
        request: Request,
        account_id: str,
        csrf_token: str = Form(default=""),
        display_name: str = Form(default=""),
    ):
        with session_scope(engine) as db:
            user, record = auth.current(request, db)
            auth.csrf(record, csrf_token)
            try:
                AccountService(db, cipher, AuditService(db)).rename_owned(
                    user.id, account_id, display_name
                )
            except NotFound:
                return _error(404, "not_found", "未找到抖音账号")
            except ValidationError:
                return _error(400, "invalid_display_name", "账号名称须为 1–64 个字符")
        return RedirectResponse("/accounts", status_code=303)

    return router
