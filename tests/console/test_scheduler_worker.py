import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.executor import ExecutionResult, ExecutionStage
from spark_console.models import DouyinAccount, SparkTask, User
from spark_console.scheduler import claim_next_due_task, compute_next_run
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.worker import Worker


class SchedulerTests(unittest.TestCase):
    def test_compute_next_run_uses_shanghai_timezone(self):
        now = datetime(2026, 8, 25, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(
            datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc),
            compute_next_run("09:00", now),
        )

    def test_same_scheduled_run_can_only_be_claimed_once(self):
        engine = create_engine("sqlite:///:memory:")
        create_schema(engine)
        session = Session(engine)
        user = User(username="friend", password_hash="hash")
        session.add(user); session.flush()
        account = DouyinAccount(owner_user_id=user.id, display_name="main", encrypted_cookies=b"x", cookie_nonce=b"n")
        session.add(account); session.flush()
        now = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
        task = SparkTask(owner_user_id=user.id, douyin_account_id=account.id, target_name="好友", send_time="09:00", message_template="今日火花", enabled=True, next_run_at=now)
        session.add(task); session.commit()
        first = claim_next_due_task(session, now, "one")
        session.commit()
        second = claim_next_due_task(session, now, "two")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(now + timedelta(days=1), task.next_run_at.replace(tzinfo=timezone.utc))
        session.close(); engine.dispose()


class _RecordingExecutor:
    def __init__(self):
        self.credential_version = None
        self.payload_reference = None

    async def execute(
        self, cookie_payload, target, message, credential_version=1
    ):
        self.credential_version = credential_version
        self.payload_reference = cookie_payload
        return ExecutionResult(True, ExecutionStage.COMPLETE)


class WorkerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_worker_passes_account_version_and_clears_decrypted_payload(self):
        now = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            cookie_key = data_dir / "cookie.key"
            session_key = data_dir / "session.key"
            cookie_key.write_bytes(b"w" * 32)
            session_key.write_bytes(b"s" * 32)
            settings = Settings(
                data_dir=data_dir,
                database_url=f"sqlite:///{data_dir / 'worker.db'}",
                cookie_key_file=cookie_key,
                session_key_file=session_key,
            )
            engine = create_engine(settings.database_url)
            create_schema(engine)
            with Session(engine) as session:
                user = User(username="worker-owner", password_hash="hash")
                session.add(user)
                session.flush()
                account = AccountService(
                    session, CookieCipher(b"w" * 32), AuditService(session)
                ).create_from_storage_state(
                    user.id,
                    "扫码账号",
                    {
                        "cookies": [
                            {"name": "sid", "value": "worker-secret-marker"}
                        ],
                        "origins": [],
                    },
                )
                task = SparkTask(
                    owner_user_id=user.id,
                    douyin_account_id=account.id,
                    target_name="好友",
                    send_time="09:00",
                    message_template="今日火花",
                    enabled=True,
                    next_run_at=now,
                )
                session.add(task)
                session.commit()

            executor = _RecordingExecutor()
            try:
                result = await Worker(settings, engine, executor=executor).run_once(now)
            finally:
                engine.dispose()
            self.assertEqual("success", result.status)
            self.assertEqual(2, executor.credential_version)
            self.assertEqual(bytearray(), executor.payload_reference)


if __name__ == "__main__":
    unittest.main()
