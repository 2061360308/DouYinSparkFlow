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
from spark_console.models import (
    DouyinAccount,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
    WorkerLock,
)
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
        self.target_sec_uid = None

    async def execute(
        self,
        cookie_payload,
        target,
        message,
        credential_version=1,
        target_sec_uid=None,
    ):
        self.credential_version = credential_version
        self.payload_reference = cookie_payload
        self.target_sec_uid = target_sec_uid
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


class _CookieInvalidExecutor:
    async def execute(self, *_args, **_kwargs):
        return ExecutionResult(
            False,
            ExecutionStage.AUTHENTICATING,
            "cookie_invalid",
            "抖音登录已失效",
            retryable=False,
        )


class _BlockingExecutor:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, *_args, **_kwargs):
        self.started.set()
        await self.release.wait()
        return ExecutionResult(True, ExecutionStage.COMPLETE)


class _HangingExecutor:
    def __init__(self):
        self.cancelled = False

    async def execute(self, *_args, **_kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class WorkerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_worker_refreshes_health_lease_even_when_no_task_is_due(self):
        now = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "cookie.key").write_bytes(b"w" * 32)
            (data_dir / "session.key").write_bytes(b"s" * 32)
            settings = Settings(
                data_dir=data_dir,
                database_url=f"sqlite:///{data_dir / 'heartbeat.db'}",
                cookie_key_file=data_dir / "cookie.key",
                session_key_file=data_dir / "session.key",
                worker_poll_seconds=10,
            )
            engine = create_engine(settings.database_url)
            create_schema(engine)
            worker = Worker(settings, engine, executor=_RecordingExecutor(), started_at=now)

            result = await worker.run_once(now)

            self.assertIsNone(result)
            with Session(engine) as session:
                lock = session.get(WorkerLock, 1)
                self.assertEqual(worker.worker_id, lock.worker_id)
                self.assertEqual(now + timedelta(seconds=30), lock.lease_until.replace(tzinfo=timezone.utc))
            engine.dispose()

    async def test_worker_marks_account_invalid_after_authentication_rejection(self):
        now = datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "cookie.key").write_bytes(b"w" * 32)
            (data_dir / "session.key").write_bytes(b"s" * 32)
            settings = Settings(data_dir=data_dir, database_url=f"sqlite:///{data_dir / 'invalid.db'}", cookie_key_file=data_dir / "cookie.key", session_key_file=data_dir / "session.key")
            engine = create_engine(settings.database_url)
            create_schema(engine)
            try:
                with Session(engine) as session:
                    user = User(username="invalid-owner", password_hash="hash")
                    session.add(user)
                    session.flush()
                    account = AccountService(session, CookieCipher(b"w" * 32), AuditService(session)).create(user.id, "登录账号", '[{"name":"sid","value":"x"}]')
                    account.validation_state = "valid"
                    task = SparkTask(owner_user_id=user.id, douyin_account_id=account.id, target_name="好友", send_time="09:00", message_template="消息", enabled=True, next_run_at=now)
                    session.add(task)
                    session.commit()
                    account_id = account.id

                result = await Worker(settings, engine, executor=_CookieInvalidExecutor(), started_at=now).run_once(now)

                self.assertEqual("cookie_invalid", result.error_code)
                with Session(engine) as session:
                    self.assertEqual("invalid", session.get(DouyinAccount, account_id).validation_state)
            finally:
                engine.dispose()

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
                session.flush()
                session.add(
                    SparkTaskTargetIdentity(
                        task_id=task.id,
                        sec_uid="stable-user-id",
                    )
                )
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
            self.assertEqual("stable-user-id", executor.target_sec_uid)
            self.assertEqual(0, len(payload))

    async def test_browser_execution_does_not_hold_sqlite_write_lock(self):
        now = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)
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
            engine = create_engine(
                settings.database_url,
                connect_args={"timeout": 0.05},
            )
            create_schema(engine)
            try:
                with Session(engine) as session:
                    owner = User(username="lock-owner", password_hash="hash")
                    session.add(owner)
                    session.flush()
                    account = AccountService(
                        session, CookieCipher(b"w" * 32), AuditService(session)
                    ).create_from_storage_state(
                        owner.id,
                        "扫码账号",
                        {
                            "cookies": [
                                {
                                    "name": "sid",
                                    "value": "lock-test-marker",
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
                            owner_user_id=owner.id,
                            douyin_account_id=account.id,
                            target_name="好友",
                            send_time="09:00",
                            message_template="今日火花",
                            enabled=True,
                            next_run_at=now,
                        )
                    )
                    session.commit()

                executor = _BlockingExecutor()
                task = asyncio.create_task(
                    Worker(
                        settings,
                        engine,
                        executor=executor,
                        started_at=now,
                    ).run_once(now)
                )
                await asyncio.wait_for(executor.started.wait(), timeout=0.2)
                try:
                    with Session(engine) as session:
                        lease = session.get(WorkerLock, 1).lease_until.replace(
                            tzinfo=timezone.utc
                        )
                        self.assertGreaterEqual(
                            lease, now + timedelta(seconds=180)
                        )
                        session.add(
                            User(username="concurrent-login", password_hash="hash")
                        )
                        session.commit()
                finally:
                    executor.release.set()
                    result = await asyncio.wait_for(task, timeout=0.2)
                self.assertEqual("success", result.status)
            finally:
                engine.dispose()

    async def test_browser_execution_timeout_is_persisted_and_cancelled(self):
        now = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)
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
                    owner = User(username="timeout-owner", password_hash="hash")
                    session.add(owner)
                    session.flush()
                    account = AccountService(
                        session, CookieCipher(b"w" * 32), AuditService(session)
                    ).create_from_storage_state(
                        owner.id,
                        "扫码账号",
                        {
                            "cookies": [
                                {
                                    "name": "sid",
                                    "value": "timeout-test-marker",
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
                            owner_user_id=owner.id,
                            douyin_account_id=account.id,
                            target_name="好友",
                            send_time="10:00",
                            message_template="今日火花",
                            enabled=True,
                            next_run_at=now,
                        )
                    )
                    session.commit()

                executor = _HangingExecutor()
                result = await Worker(
                    settings,
                    engine,
                    executor=executor,
                    started_at=now,
                    execution_timeout_seconds=0.02,
                ).run_once(now)

                self.assertTrue(executor.cancelled)
                self.assertEqual("failed", result.status)
                self.assertEqual("execution_timeout", result.error_code)
                with Session(engine) as session:
                    persisted = session.scalar(select(TaskRun))
                    self.assertEqual("failed", persisted.status)
                    self.assertEqual("execution_timeout", persisted.error_code)
            finally:
                engine.dispose()

    async def test_worker_startup_recovers_interrupted_committed_run(self):
        now = datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)
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
                    owner = User(username="restart-owner", password_hash="hash")
                    session.add(owner)
                    session.flush()
                    account = DouyinAccount(
                        owner_user_id=owner.id,
                        display_name="main",
                        encrypted_cookies=b"not-needed",
                        cookie_nonce=b"not-needed",
                    )
                    session.add(account)
                    session.flush()
                    task = SparkTask(
                        owner_user_id=owner.id,
                        douyin_account_id=account.id,
                        target_name="好友",
                        send_time="11:00",
                        message_template="今日火花",
                        enabled=True,
                        next_run_at=now + timedelta(days=1),
                    )
                    session.add(task)
                    session.flush()
                    run = TaskRun(
                        task_id=task.id,
                        scheduled_for=now - timedelta(minutes=2),
                        status="running",
                        stage="claimed",
                        started_at=now - timedelta(minutes=2),
                    )
                    session.add(run)
                    session.commit()
                    task_id = task.id
                    run_id = run.id

                Worker(settings, engine, executor=_RecordingExecutor(), started_at=now)

                with Session(engine) as session:
                    recovered_run = session.get(TaskRun, run_id)
                    recovered_task = session.get(SparkTask, task_id)
                    self.assertEqual("failed", recovered_run.status)
                    self.assertEqual("worker_interrupted", recovered_run.error_code)
                    self.assertEqual(
                        now + timedelta(minutes=1),
                        recovered_task.next_run_at.replace(tzinfo=timezone.utc),
                    )
            finally:
                engine.dispose()

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
