import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from spark_console.auth_scanner import (
    LoginTimedOut,
    QrLoadFailed,
    ScanCancelled,
    ScannedAccount,
    VerificationRequired,
)
from spark_console.auth_worker import AuthWorker
from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import (
    AuditEvent,
    DouyinAccount,
    DouyinLoginSession,
    ScanStatus,
    User,
)
from spark_console.services.scan_sessions import PNG_SIGNATURE, ScanSessionService


COOKIE_MARKER = "worker-cookie-marker"
STORAGE_MARKER = "worker-storage-marker"
EXCEPTION_MARKER = "worker-exception-marker"
QR_MARKER = "worker-qr-marker"


def _storage_state(cookies=True):
    cookie_rows = []
    if cookies:
        cookie_rows.append(
            {
                "name": "sid",
                "value": COOKIE_MARKER,
                "domain": ".douyin.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        )
    return {
        "cookies": cookie_rows,
        "origins": [
            {
                "origin": "https://www.douyin.com",
                "localStorage": [{"name": "token", "value": STORAGE_MARKER}],
            }
        ],
    }


class _LifecycleScanner:
    def __init__(
        self,
        engine,
        scan_id,
        *,
        state=None,
        cancel_before_return=False,
        expire_before_return=False,
    ):
        self.engine = engine
        self.scan_id = scan_id
        self.state = state if state is not None else _storage_state()
        self.cancel_before_return = cancel_before_return
        self.expire_before_return = expire_before_return
        self.observed_statuses = []

    async def run(self, on_qr, on_confirming, cancelled, *, expires_at=None):
        on_qr(PNG_SIGNATURE + b"worker-qr-fixture")
        with Session(self.engine) as session:
            self.observed_statuses.append(
                session.get(DouyinLoginSession, self.scan_id).status
            )
        on_confirming(True)
        with Session(self.engine) as session:
            self.observed_statuses.append(
                session.get(DouyinLoginSession, self.scan_id).status
            )
        if self.cancel_before_return:
            with session_scope(self.engine) as session:
                scan = session.get(DouyinLoginSession, self.scan_id)
                ScanSessionService(session).cancel_owned(scan.owner_user_id, scan.id)
            self.observed_statuses.append("cancelled" if cancelled() else "active")
        if self.expire_before_return:
            with session_scope(self.engine) as session:
                scan = session.get(DouyinLoginSession, self.scan_id)
                scan.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        return ScannedAccount(" 测试昵称 ", " douyin-123 ", self.state)


class _RaisingScanner:
    def __init__(self, error):
        self.error = error

    async def run(
        self, _on_qr, _on_confirming, _cancelled, *, expires_at=None
    ):
        raise self.error


class _SensitiveFailureScanner:
    async def run(
        self, on_qr, _on_confirming, _cancelled, *, expires_at=None
    ):
        on_qr(PNG_SIGNATURE + QR_MARKER.encode())
        raise RuntimeError(
            "|".join(
                (EXCEPTION_MARKER, COOKIE_MARKER, STORAGE_MARKER, QR_MARKER)
            )
        )


class _DeadlineScanner:
    def __init__(self):
        self.expires_at = None

    async def run(
        self, on_qr, _on_confirming, _cancelled, *, expires_at=None
    ):
        self.expires_at = expires_at
        on_qr(PNG_SIGNATURE + b"deadline-qr-fixture")
        if expires_at is None:
            raise LoginTimedOut()
        deadline = expires_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        remaining = max(
            0, (deadline - datetime.now(timezone.utc)).total_seconds()
        )
        await asyncio.sleep(remaining + 0.01)
        raise LoginTimedOut()


class _StopAwareScanner:
    def __init__(self):
        self.started = asyncio.Event()
        self.expires_at = None

    async def run(
        self, _on_qr, _on_confirming, cancelled, *, expires_at=None
    ):
        self.expires_at = expires_at
        self.started.set()
        while not cancelled():
            await asyncio.sleep(0)
        raise ScanCancelled()


class AuthWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0
        self.temporary = tempfile.TemporaryDirectory()
        data_dir = Path(self.temporary.name)
        cookie_key_file = data_dir / "cookie.key"
        session_key_file = data_dir / "session.key"
        cookie_key_file.write_bytes(b"k" * 32)
        session_key_file.write_bytes(b"s" * 32)
        self.settings = Settings(
            data_dir=data_dir,
            database_url=f"sqlite:///{data_dir / 'auth-worker.db'}",
            cookie_key_file=cookie_key_file,
            session_key_file=session_key_file,
            worker_poll_seconds=1,
        )
        self.engine = create_engine_for(self.settings)
        create_schema(self.engine)
        with session_scope(self.engine) as session:
            owner = User(username="auth-owner", password_hash="hash", role="user")
            session.add(owner)
            session.flush()
            self.owner_id = owner.id

    async def asyncTearDown(self):
        self.engine.dispose()
        self.temporary.cleanup()

    def _start_scan(self, *, now=None):
        with session_scope(self.engine) as session:
            service = ScanSessionService(session, now=now or (lambda: datetime.now(timezone.utc)))
            return service.start(self.owner_id).id

    def _persisted(self, scan_id):
        with Session(self.engine) as session:
            scan = session.get(DouyinLoginSession, scan_id)
            return {
                "status": scan.status,
                "slot": scan.slot,
                "qr_png": scan.qr_png,
                "account_id": scan.account_id,
                "error_code": scan.error_code,
            }

    def _set_expiry(self, scan_id, expires_at):
        with session_scope(self.engine) as session:
            session.get(DouyinLoginSession, scan_id).expires_at = expires_at

    async def test_success_uses_real_state_machine_and_encrypted_account_service(self):
        scan_id = self._start_scan()
        scanner = _LifecycleScanner(self.engine, scan_id)

        claimed = await AuthWorker(
            self.settings, self.engine, scanner=scanner
        ).run_once()

        persisted = self._persisted(scan_id)
        self.assertTrue(claimed)
        self.assertEqual(
            [ScanStatus.AWAITING_SCAN, ScanStatus.CONFIRMING],
            scanner.observed_statuses,
        )
        self.assertEqual(ScanStatus.SUCCEEDED, persisted["status"])
        self.assertIsNone(persisted["slot"])
        self.assertIsNone(persisted["qr_png"])
        self.assertIsNotNone(persisted["account_id"])
        with Session(self.engine) as session:
            account = session.get(DouyinAccount, persisted["account_id"])
            audits = session.scalars(select(AuditEvent)).all()
            self.assertEqual(2, account.cookie_version)
            self.assertEqual("valid", account.validation_state)
            self.assertFalse(COOKIE_MARKER.encode() in account.encrypted_cookies)
            self.assertFalse(STORAGE_MARKER.encode() in account.encrypted_cookies)
            self.assertFalse(
                any(
                    marker in (event.detail or "")
                    for event in audits
                    for marker in (COOKIE_MARKER, STORAGE_MARKER)
                )
            )
        self.assertEqual(0, len(scanner.state))

    async def test_typed_scanner_errors_map_to_fixed_public_codes(self):
        cases = (
            (QrLoadFailed(), "qr_load_failed"),
            (LoginTimedOut(), "login_timeout"),
            (VerificationRequired(), "verification_required"),
            (ScanCancelled(), "cancelled"),
        )
        for error, expected_code in cases:
            with self.subTest(code=expected_code):
                scan_id = self._start_scan()
                worker = AuthWorker(
                    self.settings,
                    self.engine,
                    scanner=_RaisingScanner(error),
                )

                self.assertTrue(await worker.run_once())

                persisted = self._persisted(scan_id)
                self.assertEqual(ScanStatus.FAILED, persisted["status"])
                self.assertEqual(expected_code, persisted["error_code"])
                self.assertIsNone(persisted["slot"])
                self.assertIsNone(persisted["qr_png"])

    async def test_empty_cookies_fail_before_account_creation(self):
        scan_id = self._start_scan()
        scanner = _LifecycleScanner(
            self.engine, scan_id, state=_storage_state(cookies=False)
        )

        self.assertTrue(
            await AuthWorker(self.settings, self.engine, scanner=scanner).run_once()
        )

        persisted = self._persisted(scan_id)
        self.assertEqual(ScanStatus.FAILED, persisted["status"])
        self.assertEqual("credential_invalid", persisted["error_code"])
        with Session(self.engine) as session:
            self.assertEqual(0, len(session.scalars(select(DouyinAccount)).all()))
        self.assertEqual(0, len(scanner.state))

    async def test_cancel_before_final_transaction_rolls_back_new_account(self):
        scan_id = self._start_scan()
        scanner = _LifecycleScanner(
            self.engine, scan_id, cancel_before_return=True
        )

        self.assertTrue(
            await AuthWorker(self.settings, self.engine, scanner=scanner).run_once()
        )

        persisted = self._persisted(scan_id)
        self.assertEqual("cancelled", scanner.observed_statuses[-1])
        self.assertEqual(ScanStatus.CANCELLED, persisted["status"])
        self.assertEqual("cancelled", persisted["error_code"])
        self.assertIsNone(persisted["account_id"])
        with Session(self.engine) as session:
            self.assertEqual(0, len(session.scalars(select(DouyinAccount)).all()))
        self.assertEqual(0, len(scanner.state))

    async def test_expiry_at_final_transaction_rolls_back_new_account(self):
        scan_id = self._start_scan()
        scanner = _LifecycleScanner(
            self.engine, scan_id, expire_before_return=True
        )

        self.assertTrue(
            await AuthWorker(self.settings, self.engine, scanner=scanner).run_once()
        )

        persisted = self._persisted(scan_id)
        self.assertEqual(ScanStatus.EXPIRED, persisted["status"])
        self.assertEqual("login_timeout", persisted["error_code"])
        self.assertIsNone(persisted["account_id"])
        with Session(self.engine) as session:
            self.assertEqual(0, len(session.scalars(select(DouyinAccount)).all()))
        self.assertEqual(0, len(scanner.state))

    async def test_unknown_error_log_contains_only_session_and_public_code(self):
        scan_id = self._start_scan()
        worker = AuthWorker(
            self.settings,
            self.engine,
            scanner=_SensitiveFailureScanner(),
        )

        with self.assertLogs("spark_console.auth_worker", level="WARNING") as captured:
            self.assertTrue(await worker.run_once())

        persisted = self._persisted(scan_id)
        self.assertEqual("automation_failed", persisted["error_code"])
        self.assertTrue(any(scan_id in line for line in captured.output))
        self.assertTrue(any("automation_failed" in line for line in captured.output))
        self.assertFalse(any(EXCEPTION_MARKER in line for line in captured.output))
        self.assertFalse(any(COOKIE_MARKER in line for line in captured.output))
        self.assertFalse(any(STORAGE_MARKER in line for line in captured.output))
        self.assertFalse(any(QR_MARKER in line for line in captured.output))
        self.assertIsNone(persisted["qr_png"])

    async def test_startup_expires_abandoned_session(self):
        old_now = datetime.now(timezone.utc) - timedelta(minutes=10)
        scan_id = self._start_scan(now=lambda: old_now)

        AuthWorker(self.settings, self.engine, scanner=_RaisingScanner(QrLoadFailed()))

        persisted = self._persisted(scan_id)
        self.assertEqual(ScanStatus.EXPIRED, persisted["status"])
        self.assertEqual("login_timeout", persisted["error_code"])
        self.assertIsNone(persisted["slot"])

    async def test_run_once_returns_false_when_no_scan_is_queued(self):
        worker = AuthWorker(
            self.settings, self.engine, scanner=_RaisingScanner(QrLoadFailed())
        )

        self.assertFalse(await worker.run_once())

    async def test_persisted_expiry_drives_timeout_and_terminal_cleanup(self):
        scan_id = self._start_scan()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=0.05)
        self._set_expiry(scan_id, expires_at)
        scanner = _DeadlineScanner()

        self.assertTrue(
            await AuthWorker(self.settings, self.engine, scanner=scanner).run_once()
        )

        persisted = self._persisted(scan_id)
        self.assertIsNotNone(scanner.expires_at)
        self.assertEqual(
            expires_at.replace(tzinfo=None),
            scanner.expires_at.replace(tzinfo=None),
        )
        self.assertEqual(ScanStatus.EXPIRED, persisted["status"])
        self.assertEqual("login_timeout", persisted["error_code"])
        self.assertIsNone(persisted["slot"])
        self.assertIsNone(persisted["qr_png"])

    async def test_stopping_event_cancels_active_scan_and_releases_slot(self):
        scan_id = self._start_scan()
        scanner = _StopAwareScanner()
        stopping = asyncio.Event()
        worker = AuthWorker(self.settings, self.engine, scanner=scanner)
        task = asyncio.create_task(worker.run_once(stopping))
        await asyncio.wait_for(scanner.started.wait(), timeout=0.2)

        stopping.set()

        self.assertTrue(await asyncio.wait_for(task, timeout=0.2))
        persisted = self._persisted(scan_id)
        self.assertIsNotNone(scanner.expires_at)
        self.assertEqual(ScanStatus.FAILED, persisted["status"])
        self.assertEqual("cancelled", persisted["error_code"])
        self.assertIsNone(persisted["slot"])
        self.assertIsNone(persisted["qr_png"])


if __name__ == "__main__":
    unittest.main()
