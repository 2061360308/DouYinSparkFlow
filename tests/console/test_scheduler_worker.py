import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.executor import ExecutionResult, ExecutionStage
from spark_console.models import DouyinAccount, SparkTask, TaskRun, User
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


class _RaisingExecutor:
    async def execute(self, *_args, **_kwargs):
        raise RuntimeError("sensitive-worker-exception")


class _RetryableExecutor:
    async def execute(self, *_args, **_kwargs):
        return ExecutionResult(
            False,
            ExecutionStage.AUTHENTICATING,
            "network_unavailable",
            "网络暂时不可用",
            retryable=True,
        )


class WorkerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_worker_passes_account_version_and_clears_decrypted_payload(self):
        now = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
        scheduled = now - timedelta(seconds=5)
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
                            {
                                "name": "sid",
                                "value": "worker-secret-marker",
                                "domain": ".douyin.com",
                                "path": "/",
                                "expires": -1,
                                "httpOnly": True,
                                "secure": True,
                                "sameSite": "Lax",
                            }
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
                    next_run_at=scheduled,
                )
                session.add(task)
                session.commit()

            executor = _RecordingExecutor()
            payload = bytearray(b"worker-secret-marker")
            decrypted = False
            original_get = Session.get

            def decrypt_for_worker(_service, _account_id):
                nonlocal decrypted
                decrypted = True
                return payload

            def reject_post_decrypt_lookup(db, entity, ident, **kwargs):
                if entity is DouyinAccount and decrypted:
                    raise RuntimeError("database lookup after credential decryption")
                return original_get(db, entity, ident, **kwargs)

            try:
                with patch.object(
                    AccountService, "decrypt_for_worker", decrypt_for_worker
                ), patch.object(Session, "get", reject_post_decrypt_lookup):
                    result = await Worker(
                        settings, engine, executor=executor, started_at=now
                    ).run_once(now)
            finally:
                engine.dispose()
            self.assertEqual("success", result.status)
            self.assertEqual(2, executor.credential_version)
            self.assertEqual(0, len(payload))

    async def test_retryable_failure_retries_after_one_then_five_minutes(self):
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
            try:
                with Session(engine) as session:
                    user = User(username="retry-owner", password_hash="hash")
                    session.add(user)
                    session.flush()
                    account = AccountService(
                        session, CookieCipher(b"w" * 32), AuditService(session)
                    ).create_from_storage_state(
                        user.id,
                        "扫码账号",
                        {
                            "cookies": [
                                {
                                    "name": "sid",
                                    "value": "retry-secret-marker",
                                    "domain": ".douyin.com",
                                    "path": "/",
                                    "expires": -1,
                                    "httpOnly": True,
                                    "secure": True,
                                    "sameSite": "Lax",
                                }
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
                    task_id = task.id

                worker = Worker(
                    settings,
                    engine,
                    executor=_RetryableExecutor(),
                    started_at=now,
                )
                first = await worker.run_once(now)
                self.assertEqual("retry_scheduled_1m", first.error_code)
                with Session(engine) as session:
                    self.assertEqual(
                        now + timedelta(minutes=1),
                        session.get(SparkTask, task_id).next_run_at.replace(tzinfo=timezone.utc),
                    )

                second_time = now + timedelta(minutes=1)
                second = await worker.run_once(second_time)
                self.assertEqual("retry_scheduled_5m", second.error_code)
                with Session(engine) as session:
                    self.assertEqual(
                        second_time + timedelta(minutes=5),
                        session.get(SparkTask, task_id).next_run_at.replace(tzinfo=timezone.utc),
                    )

                third_time = second_time + timedelta(minutes=5)
                third = await worker.run_once(third_time)
                self.assertEqual("network_unavailable", third.error_code)
                with Session(engine) as session:
                    task = session.get(SparkTask, task_id)
                    self.assertGreater(
                        task.next_run_at.replace(tzinfo=timezone.utc),
                        third_time + timedelta(hours=20),
                    )
                    self.assertEqual(3, len(session.scalars(select(TaskRun)).all()))
            finally:
                engine.dispose()

    async def test_worker_startup_records_but_never_sends_tasks_missed_while_offline(self):
        scheduled = datetime(2026, 8, 26, 8, 36, tzinfo=timezone.utc)
        started = scheduled + timedelta(minutes=20)
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
                user = User(username="offline-owner", password_hash="hash")
                session.add(user)
                session.flush()
                account = DouyinAccount(
                    owner_user_id=user.id,
                    display_name="main",
                    encrypted_cookies=b"not-needed",
                    cookie_nonce=b"not-needed",
                )
                session.add(account)
                session.flush()
                task = SparkTask(
                    owner_user_id=user.id,
                    douyin_account_id=account.id,
                    target_name="好友",
                    send_time="16:36",
                    message_template="今日火花",
                    enabled=True,
                    next_run_at=scheduled,
                )
                session.add(task)
                session.commit()

            executor = _RecordingExecutor()
            result = await Worker(
                settings,
                engine,
                executor=executor,
                started_at=started,
            ).run_once(started)

            self.assertEqual("skipped", result.status)
            self.assertEqual("missed_startup", result.stage)
            self.assertEqual("worker_was_offline", result.error_code)
            self.assertIsNone(executor.payload_reference)
            with Session(engine) as session:
                persisted = session.scalar(select(TaskRun))
                self.assertEqual("skipped", persisted.status)
            engine.dispose()

    async def test_unexpected_executor_error_is_recorded_without_crashing_worker(self):
        now = datetime(2026, 8, 26, 9, 0, 5, tzinfo=timezone.utc)
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
            try:
                with Session(engine) as session:
                    user = User(username="error-owner", password_hash="hash")
                    session.add(user)
                    session.flush()
                    account = AccountService(
                        session, CookieCipher(b"w" * 32), AuditService(session)
                    ).create_from_storage_state(
                        user.id,
                        "扫码账号",
                        {
                            "cookies": [
                                {
                                    "name": "sid",
                                    "value": "worker-secret-marker",
                                    "domain": ".douyin.com",
                                    "path": "/",
                                    "expires": -1,
                                    "httpOnly": True,
                                    "secure": True,
                                    "sameSite": "Lax",
                                }
                            ],
                            "origins": [],
                        },
                    )
                    session.add(
                        SparkTask(
                            owner_user_id=user.id,
                            douyin_account_id=account.id,
                            target_name="好友",
                            send_time="17:00",
                            message_template="今日火花",
                            enabled=True,
                            next_run_at=now - timedelta(seconds=5),
                        )
                    )
                    session.commit()

                result = await Worker(
                    settings,
                    engine,
                    executor=_RaisingExecutor(),
                    started_at=now - timedelta(minutes=1),
                ).run_once(now)

                self.assertEqual("failed", result.status)
                self.assertEqual("worker_error", result.stage)
                self.assertEqual("unexpected_error", result.error_code)
                self.assertNotIn("sensitive-worker-exception", result.error_summary)
                with Session(engine) as session:
                    persisted = session.scalar(select(TaskRun))
                    self.assertEqual("failed", persisted.status)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
