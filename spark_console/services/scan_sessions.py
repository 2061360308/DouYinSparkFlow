from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import DouyinLoginSession, ScanStatus, utc_now
from spark_console.services import Conflict, NotFound, ValidationError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_QR_PNG_BYTES = 1024 * 1024

ACTIVE_STATUSES = frozenset(
    {
        ScanStatus.QUEUED,
        ScanStatus.LOADING_QR,
        ScanStatus.AWAITING_SCAN,
        ScanStatus.CONFIRMING,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        ScanStatus.SUCCEEDED,
        ScanStatus.FAILED,
        ScanStatus.EXPIRED,
        ScanStatus.CANCELLED,
    }
)
ALLOWED = {
    ScanStatus.QUEUED: {
        ScanStatus.LOADING_QR,
        ScanStatus.CANCELLED,
        ScanStatus.EXPIRED,
        ScanStatus.FAILED,
    },
    ScanStatus.LOADING_QR: {
        ScanStatus.AWAITING_SCAN,
        ScanStatus.CANCELLED,
        ScanStatus.EXPIRED,
        ScanStatus.FAILED,
    },
    ScanStatus.AWAITING_SCAN: {
        ScanStatus.CONFIRMING,
        ScanStatus.SUCCEEDED,
        ScanStatus.CANCELLED,
        ScanStatus.EXPIRED,
        ScanStatus.FAILED,
    },
    ScanStatus.CONFIRMING: {
        ScanStatus.SUCCEEDED,
        ScanStatus.CANCELLED,
        ScanStatus.EXPIRED,
        ScanStatus.FAILED,
    },
}

FAILURE_CODES = frozenset(
    {
        "qr_load_failed",
        "login_timeout",
        "cancelled",
        "verification_required",
        "credential_invalid",
        "automation_failed",
    }
)

STATUS_MESSAGES = {
    ScanStatus.QUEUED: "等待开始扫码",
    ScanStatus.LOADING_QR: "正在加载二维码",
    ScanStatus.AWAITING_SCAN: "请使用抖音 App 扫码并在手机确认",
    ScanStatus.CONFIRMING: "已扫码，请在手机确认",
    ScanStatus.SUCCEEDED: "绑定成功",
    ScanStatus.FAILED: "绑定失败，请重试",
    ScanStatus.EXPIRED: "扫码已超时，请重试",
    ScanStatus.CANCELLED: "扫码已取消",
}
ERROR_MESSAGES = {
    "qr_load_failed": "二维码加载失败，请重试",
    "login_timeout": "扫码已超时，请重试",
    "cancelled": "扫码已取消",
    "verification_required": "抖音要求额外验证，请稍后重试",
    "credential_invalid": "登录凭证无效，请重试",
    "automation_failed": "自动化登录失败，请重试",
}


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ScanSessionService:
    def __init__(
        self,
        session: Session,
        now=utc_now,
        lifetime: timedelta = timedelta(minutes=5),
    ):
        self.session = session
        self.now = now
        self.lifetime = lifetime

    def start(self, owner_id: str) -> DouyinLoginSession:
        current = self._now()
        scan = DouyinLoginSession(
            owner_user_id=owner_id,
            slot="global",
            status=ScanStatus.QUEUED,
            expires_at=current + self.lifetime,
            created_at=current,
            updated_at=current,
        )
        try:
            with self.session.begin_nested():
                self.session.add(scan)
                self.session.flush()
        except IntegrityError:
            raise Conflict("slot_busy") from None
        return scan

    def claim_next(self) -> DouyinLoginSession | None:
        scan_id = self.session.scalar(
            select(DouyinLoginSession.id)
            .where(
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status == ScanStatus.QUEUED,
            )
            .order_by(DouyinLoginSession.created_at, DouyinLoginSession.id)
            .limit(1)
        )
        if scan_id is None:
            return None
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.id == scan_id,
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status == ScanStatus.QUEUED,
            )
            .values(status=ScanStatus.LOADING_QR, updated_at=self._now())
        )
        if result.rowcount != 1:
            return None
        self.session.flush()
        return self._get(scan_id)

    def publish_qr(self, scan_id: str, png: bytes) -> DouyinLoginSession:
        if (
            not isinstance(png, bytes)
            or not png.startswith(PNG_SIGNATURE)
            or len(png) > MAX_QR_PNG_BYTES
        ):
            raise ValidationError("invalid_qr_png")
        scan = self._get(scan_id)
        self._transition(scan, ScanStatus.AWAITING_SCAN)
        scan.qr_png = png
        self.session.flush()
        return scan

    def mark_confirming(self, scan_id: str) -> DouyinLoginSession:
        scan = self._get(scan_id)
        self._transition(scan, ScanStatus.CONFIRMING)
        self.session.flush()
        return scan

    def complete(self, scan_id: str, account_id: str) -> DouyinLoginSession:
        scan = self._get(scan_id)
        return self._terminal(
            scan,
            ScanStatus.SUCCEEDED,
            account_id=account_id,
        )

    def fail(self, scan_id: str, code: str) -> DouyinLoginSession:
        if code not in FAILURE_CODES:
            raise ValidationError("invalid_error_code")
        scan = self._get(scan_id)
        return self._terminal(scan, ScanStatus.FAILED, error_code=code)

    def cancel_owned(self, owner_id: str, scan_id: str) -> DouyinLoginSession:
        scan = self.get_owned(owner_id, scan_id)
        return self._terminal(
            scan,
            ScanStatus.CANCELLED,
            error_code="cancelled",
        )

    def get_owned(self, owner_id: str, scan_id: str) -> DouyinLoginSession:
        scan = self.session.scalar(
            select(DouyinLoginSession).where(
                DouyinLoginSession.id == scan_id,
                DouyinLoginSession.owner_user_id == owner_id,
            )
        )
        if scan is None:
            raise NotFound("scan session not found")
        return scan

    def expire_stale(self) -> int:
        stale = self.session.scalars(
            select(DouyinLoginSession).where(
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(ACTIVE_STATUSES),
                DouyinLoginSession.expires_at <= self._now(),
            )
        ).all()
        for scan in stale:
            self._terminal(
                scan,
                ScanStatus.EXPIRED,
                error_code="login_timeout",
                flush=False,
            )
        if stale:
            self.session.flush()
        return len(stale)

    def public_status(
        self, scan: DouyinLoginSession, now: datetime | None = None
    ) -> dict[str, object]:
        status = self._status(scan)
        current = _aware(now) if now is not None else self._now()
        if status in TERMINAL_STATUSES:
            remaining_seconds = 0
        else:
            remaining_seconds = max(
                0,
                math.ceil((_aware(scan.expires_at) - current).total_seconds()),
            )
        return {
            "id": scan.id,
            "status": status.value,
            "remaining_seconds": remaining_seconds,
            "error": scan.error_code,
            "message": ERROR_MESSAGES.get(
                scan.error_code, STATUS_MESSAGES[status]
            ),
            "account_id": scan.account_id,
        }

    def _get(self, scan_id: str) -> DouyinLoginSession:
        scan = self.session.get(DouyinLoginSession, scan_id)
        if scan is None:
            raise NotFound("scan session not found")
        return scan

    def _transition(
        self, scan: DouyinLoginSession, target: ScanStatus
    ) -> DouyinLoginSession:
        current = self._status(scan)
        if target not in ALLOWED.get(current, set()):
            raise Conflict("invalid_transition")
        scan.status = target
        scan.updated_at = self._now()
        return scan

    def _terminal(
        self,
        scan: DouyinLoginSession,
        target: ScanStatus,
        *,
        account_id: str | None = None,
        error_code: str | None = None,
        flush: bool = True,
    ) -> DouyinLoginSession:
        if target not in TERMINAL_STATUSES:
            raise ValueError("terminal status required")
        self._transition(scan, target)
        finished_at = self._now()
        scan.slot = None
        scan.qr_png = None
        scan.account_id = account_id
        scan.error_code = error_code
        scan.finished_at = finished_at
        scan.updated_at = finished_at
        if flush:
            self.session.flush()
        return scan

    def _now(self) -> datetime:
        return _aware(self.now())

    @staticmethod
    def _status(scan: DouyinLoginSession) -> ScanStatus:
        return ScanStatus(scan.status)
