from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from spark_console.crypto import CookieCipher
from spark_console.models import (
    DouyinLoginAction,
    DouyinLoginInput,
    DouyinLoginSession,
    ScanStatus,
    utc_now,
)
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
    ScanStatus.CONFIRMING: "已扫码；确认后正在抓取并加密保存登录凭证",
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

    def active_owned(self, owner_id: str) -> DouyinLoginSession | None:
        return self.session.scalar(
            select(DouyinLoginSession)
            .where(
                DouyinLoginSession.owner_user_id == owner_id,
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(ACTIVE_STATUSES),
            )
            .order_by(DouyinLoginSession.created_at.desc())
            .limit(1)
        )

    def claim_next(self) -> DouyinLoginSession | None:
        scan = self.session.scalar(
            select(DouyinLoginSession)
            .where(
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status == ScanStatus.QUEUED,
            )
            .order_by(DouyinLoginSession.created_at, DouyinLoginSession.id)
            .limit(1)
        )
        if scan is None:
            return None
        current = self._now()
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.id == scan.id,
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status == ScanStatus.QUEUED,
                DouyinLoginSession.expires_at > current,
            )
            .values(status=ScanStatus.LOADING_QR, updated_at=current)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._expire_one(scan, current)
            return None
        return self._reload(scan.id)

    def publish_qr(self, scan_id: str, png: bytes) -> DouyinLoginSession:
        if (
            not isinstance(png, bytes)
            or not png.startswith(PNG_SIGNATURE)
            or len(png) > MAX_QR_PNG_BYTES
        ):
            raise ValidationError("invalid_qr_png")
        scan = self._get(scan_id)
        return self._cas_transition(
            scan,
            ScanStatus.AWAITING_SCAN,
            qr_png=png,
        )

    def publish_view(self, scan_id: str, png: bytes) -> DouyinLoginSession:
        self._validate_png(png)
        scan = self._get(scan_id)
        current = self._now()
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.id == scan.id,
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(ACTIVE_STATUSES),
                DouyinLoginSession.expires_at > current,
            )
            .values(qr_png=png)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise Conflict("transition_conflict")
        return self._reload(scan.id)

    def queue_click(
        self, owner_id: str, scan_id: str, x: float, y: float
    ) -> DouyinLoginAction:
        if not all(isinstance(value, (int, float)) for value in (x, y)):
            raise ValidationError("invalid_interaction")
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValidationError("invalid_interaction")
        scan = self.get_owned(owner_id, scan_id)
        if scan.slot != "global" or self._status(scan) not in ACTIVE_STATUSES:
            raise Conflict("scan_not_active")
        action = DouyinLoginAction(
            scan_id=scan.id,
            kind="click",
            x_million=round(x * 1_000_000),
            y_million=round(y * 1_000_000),
            created_at=self._now(),
        )
        self.session.add(action)
        self.session.flush()
        return action

    def queue_text(
        self,
        owner_id: str,
        scan_id: str,
        value: str,
        cipher: CookieCipher,
    ) -> DouyinLoginInput:
        if not isinstance(value, str) or not value.isdigit() or not 4 <= len(value) <= 8:
            raise ValidationError("invalid_interaction")
        scan = self.get_owned(owner_id, scan_id)
        if scan.slot != "global" or self._status(scan) not in ACTIVE_STATUSES:
            raise Conflict("scan_not_active")
        plaintext = bytearray(value.encode("ascii"))
        try:
            encrypted = cipher.encrypt(bytes(plaintext))
        finally:
            plaintext[:] = b"\0" * len(plaintext)
            plaintext.clear()
        pending = DouyinLoginInput(
            scan_id=scan.id,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            created_at=self._now(),
        )
        self.session.add(pending)
        self.session.flush()
        return pending

    def claim_interaction(
        self, scan_id: str, cipher: CookieCipher | None = None
    ) -> dict[str, object] | None:
        action = self.session.scalar(
            select(DouyinLoginAction)
            .where(
                DouyinLoginAction.scan_id == scan_id,
                DouyinLoginAction.consumed_at.is_(None),
            )
            .order_by(DouyinLoginAction.id)
            .limit(1)
        )
        if action is not None:
            action.consumed_at = self._now()
            self.session.flush()
            return {
                "kind": action.kind,
                "x": action.x_million / 1_000_000,
                "y": action.y_million / 1_000_000,
            }
        if cipher is None:
            return None
        pending = self.session.scalar(
            select(DouyinLoginInput)
            .where(
                DouyinLoginInput.scan_id == scan_id,
                DouyinLoginInput.consumed_at.is_(None),
                DouyinLoginInput.ciphertext.is_not(None),
                DouyinLoginInput.nonce.is_not(None),
            )
            .order_by(DouyinLoginInput.id)
            .limit(1)
        )
        if pending is None or pending.ciphertext is None or pending.nonce is None:
            return None
        plaintext = bytearray(cipher.decrypt(pending.ciphertext, pending.nonce))
        try:
            value = plaintext.decode("ascii")
        finally:
            plaintext[:] = b"\0" * len(plaintext)
            plaintext.clear()
        pending.ciphertext = None
        pending.nonce = None
        pending.consumed_at = self._now()
        self.session.flush()
        return {"kind": "text", "text": value}

    def mark_confirming(self, scan_id: str) -> DouyinLoginSession:
        scan = self._get(scan_id)
        return self._cas_transition(scan, ScanStatus.CONFIRMING)

    def complete(self, scan_id: str, account_id: str) -> DouyinLoginSession:
        scan = self._get(scan_id)
        return self._cas_terminal(
            scan,
            ScanStatus.SUCCEEDED,
            account_id=account_id,
        )

    def fail(self, scan_id: str, code: str) -> DouyinLoginSession:
        if code not in FAILURE_CODES:
            raise ValidationError("invalid_error_code")
        scan = self._get(scan_id)
        return self._cas_terminal(scan, ScanStatus.FAILED, error_code=code)

    def cancel_owned(self, owner_id: str, scan_id: str) -> DouyinLoginSession:
        scan = self.get_owned(owner_id, scan_id)
        return self._cas_terminal(
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
        current = self._now()
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(ACTIVE_STATUSES),
                DouyinLoginSession.expires_at <= current,
            )
            .values(
                status=ScanStatus.EXPIRED,
                slot=None,
                qr_png=None,
                account_id=None,
                error_code="login_timeout",
                finished_at=current,
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            self._reload_cached_scans()
        return result.rowcount

    def fail_abandoned_browser_sessions(self) -> int:
        current = self._now()
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(
                    (
                        ScanStatus.LOADING_QR,
                        ScanStatus.AWAITING_SCAN,
                        ScanStatus.CONFIRMING,
                    )
                ),
            )
            .values(
                status=ScanStatus.FAILED,
                slot=None,
                qr_png=None,
                account_id=None,
                error_code="automation_failed",
                finished_at=current,
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            self._reload_cached_scans()
        return result.rowcount

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
        public_error = scan.error_code
        if public_error is not None and public_error not in FAILURE_CODES:
            public_error = "automation_failed"
        return {
            "id": scan.id,
            "status": status.value,
            "remaining_seconds": remaining_seconds,
            "error": public_error,
            "message": ERROR_MESSAGES.get(
                public_error, STATUS_MESSAGES[status]
            ),
            "account_id": scan.account_id,
        }

    def _get(self, scan_id: str) -> DouyinLoginSession:
        scan = self.session.get(DouyinLoginSession, scan_id)
        if scan is None:
            raise NotFound("scan session not found")
        return scan

    @staticmethod
    def _validate_png(png: bytes) -> None:
        if (
            not isinstance(png, bytes)
            or not png.startswith(PNG_SIGNATURE)
            or len(png) > MAX_QR_PNG_BYTES
        ):
            raise ValidationError("invalid_qr_png")

    def _cas_transition(
        self,
        scan: DouyinLoginSession,
        target: ScanStatus,
        **values,
    ) -> DouyinLoginSession:
        current = self._now()
        sources = tuple(
            source for source, targets in ALLOWED.items() if target in targets
        )
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.id == scan.id,
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(sources),
                DouyinLoginSession.expires_at > current,
            )
            .values(status=target, updated_at=current, **values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            return self._reload(scan.id)
        expired = self._expire_one(scan, current)
        if expired is not None:
            return expired
        persisted = self._reload(scan.id)
        persisted_status = self._status(persisted)
        if (
            persisted.slot == "global"
            and persisted_status in ACTIVE_STATUSES
            and target not in ALLOWED.get(persisted_status, set())
        ):
            raise Conflict("invalid_transition")
        raise Conflict("transition_conflict")

    def _cas_terminal(
        self,
        scan: DouyinLoginSession,
        target: ScanStatus,
        *,
        account_id: str | None = None,
        error_code: str | None = None,
    ) -> DouyinLoginSession:
        if target not in TERMINAL_STATUSES:
            raise ValueError("terminal status required")
        current = self._now()
        return self._cas_transition(
            scan,
            target,
            slot=None,
            qr_png=None,
            account_id=account_id,
            error_code=error_code,
            finished_at=current,
        )

    def _expire_one(
        self, scan: DouyinLoginSession, current: datetime
    ) -> DouyinLoginSession | None:
        result = self.session.execute(
            update(DouyinLoginSession)
            .where(
                DouyinLoginSession.id == scan.id,
                DouyinLoginSession.slot == "global",
                DouyinLoginSession.status.in_(ACTIVE_STATUSES),
                DouyinLoginSession.expires_at <= current,
            )
            .values(
                status=ScanStatus.EXPIRED,
                slot=None,
                qr_png=None,
                account_id=None,
                error_code="login_timeout",
                finished_at=current,
                updated_at=current,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        return self._reload(scan.id)

    def _reload(self, scan_id: str) -> DouyinLoginSession:
        scan = self.session.scalar(
            select(DouyinLoginSession)
            .where(DouyinLoginSession.id == scan_id)
            .execution_options(populate_existing=True)
        )
        if scan is None:
            raise NotFound("scan session not found")
        for attribute in ("expires_at", "created_at", "updated_at", "finished_at"):
            value = getattr(scan, attribute)
            if value is not None and value.tzinfo is None:
                set_committed_value(scan, attribute, _aware(value))
        return scan

    def _reload_cached_scans(self) -> None:
        for instance in list(self.session.identity_map.values()):
            if isinstance(instance, DouyinLoginSession):
                self._reload(instance.id)

    def _now(self) -> datetime:
        return _aware(self.now())

    @staticmethod
    def _status(scan: DouyinLoginSession) -> ScanStatus:
        return ScanStatus(scan.status)
