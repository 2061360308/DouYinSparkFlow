from __future__ import annotations

import asyncio
import logging
import os
import signal

from sqlalchemy.orm import Session

from spark_console.auth_scanner import (
    DouyinQrScanner,
    LoginTimedOut,
    QrLoadFailed,
    ScanCancelled,
    VerificationRequired,
)
from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import DouyinLoginSession, ScanStatus
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.scan_sessions import ACTIVE_STATUSES, ScanSessionService


logger = logging.getLogger(__name__)

ERROR_CODES = {
    QrLoadFailed: "qr_load_failed",
    LoginTimedOut: "login_timeout",
    VerificationRequired: "verification_required",
    ScanCancelled: "cancelled",
}


class _CredentialInvalid(Exception):
    pass


class AuthWorker:
    def __init__(self, settings: Settings, engine, scanner=None):
        self.settings = settings
        self.engine = engine
        self.scanner = scanner or DouyinQrScanner()
        key = bytearray(settings.cookie_key_file.read_bytes())
        try:
            self.cipher = CookieCipher(bytes(key))
        finally:
            key[:] = b"\0" * len(key)
            key.clear()
        with session_scope(self.engine) as db:
            service = ScanSessionService(db)
            service.expire_stale()
            service.fail_abandoned_browser_sessions()

    async def run_once(self, stopping: asyncio.Event | None = None) -> bool:
        with session_scope(self.engine) as db:
            scan = ScanSessionService(db).claim_next()
            if scan is None:
                return False
            scan_id = scan.id
            owner_id = scan.owner_user_id
            expires_at = scan.expires_at

        def on_qr(png: bytes) -> None:
            with session_scope(self.engine) as db:
                ScanSessionService(db).publish_qr(scan_id, png)

        def on_confirming(_confirmed: bool) -> None:
            with session_scope(self.engine) as db:
                ScanSessionService(db).mark_confirming(scan_id)

        def on_view(png: bytes) -> None:
            with session_scope(self.engine) as db:
                ScanSessionService(db).publish_view(scan_id, png)

        def next_interaction():
            with session_scope(self.engine) as db:
                return ScanSessionService(db).claim_interaction(scan_id, self.cipher)

        def cancelled() -> bool:
            if stopping is not None and stopping.is_set():
                return True
            with Session(self.engine) as db:
                persisted = db.get(DouyinLoginSession, scan_id)
                return (
                    persisted is None
                    or persisted.slot != "global"
                    or ScanStatus(persisted.status) not in ACTIVE_STATUSES
                )

        scanned = None
        storage_state = None
        try:
            scanned = await self.scanner.run(
                on_qr,
                on_confirming,
                cancelled,
                expires_at=expires_at,
                on_view=on_view,
                next_interaction=next_interaction,
            )
            if stopping is not None and stopping.is_set():
                raise ScanCancelled()
            storage_state = scanned.storage_state
            if (
                not isinstance(storage_state, dict)
                or not isinstance(storage_state.get("cookies"), list)
                or not storage_state["cookies"]
            ):
                raise _CredentialInvalid()

            with session_scope(self.engine) as db:
                account = AccountService(
                    db, self.cipher, AuditService(db)
                ).create_from_storage_state(
                    owner_id,
                    scanned.display_name,
                    storage_state,
                    scanned.unique_id,
                    scanned.conversation_names,
                    scanned.contact_identities,
                )
                completed = ScanSessionService(db).complete(scan_id, account.id)
                if ScanStatus(completed.status) == ScanStatus.EXPIRED:
                    raise LoginTimedOut()
                if (
                    ScanStatus(completed.status) != ScanStatus.SUCCEEDED
                    or completed.account_id != account.id
                ):
                    raise Conflict("transition_conflict")
            return True
        except tuple(ERROR_CODES) as error:
            code = ERROR_CODES[type(error)]
        except (_CredentialInvalid, ValidationError):
            code = "credential_invalid"
        except Exception:
            code = "automation_failed"
        finally:
            if isinstance(storage_state, dict):
                storage_state.clear()
            storage_state = None
            scanned = None

        self._record_failure(scan_id, code)
        logger.warning("auth scan ended session_id=%s code=%s", scan_id, code)
        return True

    def _record_failure(self, scan_id: str, code: str) -> None:
        try:
            with session_scope(self.engine) as db:
                scan = db.get(DouyinLoginSession, scan_id)
                if (
                    scan is None
                    or scan.slot != "global"
                    or ScanStatus(scan.status) not in ACTIVE_STATUSES
                ):
                    return
                ScanSessionService(db).fail(scan_id, code)
        except (Conflict, NotFound):
            return


async def run_loop() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    worker = AuthWorker(settings, engine)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            pass
    while not stopping.is_set():
        await worker.run_once(stopping)
        try:
            await asyncio.wait_for(
                stopping.wait(), timeout=settings.worker_poll_seconds
            )
        except TimeoutError:
            continue


if __name__ == "__main__":
    asyncio.run(run_loop())
