from __future__ import annotations

import asyncio
import os
import signal
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.executor import DouyinExecutor
from spark_console.models import DouyinAccount, SparkTask
from spark_console.scheduler import claim_next_due_task, finish_run
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService


class Worker:
    def __init__(self, settings: Settings, engine, executor=None, clock_offset_seconds=0.0):
        self.settings = settings
        self.engine = engine
        self.executor = executor or DouyinExecutor()
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        self.clock_offset_seconds = clock_offset_seconds
        self.cipher = CookieCipher(settings.cookie_key_file.read_bytes())

    async def run_once(self, now: datetime | None = None):
        current_time = now or datetime.now(timezone.utc)
        with session_scope(self.engine) as db:
            run = claim_next_due_task(db, current_time, self.worker_id)
            if run is None:
                return None
            task = db.get(SparkTask, run.task_id)
            if abs(self.clock_offset_seconds) > self.settings.clock_offset_limit_seconds:
                return finish_run(run, "failed", "clock_check", current_time, "system_time_unhealthy", "服务器时间未同步")
            scheduled = run.scheduled_for if run.scheduled_for.tzinfo else run.scheduled_for.replace(tzinfo=timezone.utc)
            if current_time - scheduled > timedelta(minutes=10):
                return finish_run(run, "skipped", "late", current_time, "missed_window", "任务已超过 10 分钟发送窗口")
            account_service = AccountService(db, self.cipher, AuditService(db))
            account = db.get(DouyinAccount, task.douyin_account_id)
            credential_version = account.cookie_version
            cookies = account_service.decrypt_for_worker(task.douyin_account_id)
            try:
                result = await self.executor.execute(
                    cookies,
                    task.target_name,
                    task.message_template,
                    credential_version=credential_version,
                )
            finally:
                cookies[:] = b"\0" * len(cookies)
                cookies.clear()
            return finish_run(
                run,
                "success" if result.success else "failed",
                result.stage,
                datetime.now(timezone.utc),
                result.error_code,
                result.error_summary,
            )


async def run_loop() -> None:
    settings = Settings.from_env(os.environ)
    engine = create_engine_for(settings)
    create_schema(engine)
    worker = Worker(settings, engine, clock_offset_seconds=float(os.environ.get("SPARK_CLOCK_OFFSET_SECONDS", "0")))
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            pass
    while not stopping.is_set():
        await worker.run_once()
        try:
            await asyncio.wait_for(stopping.wait(), timeout=settings.worker_poll_seconds)
        except TimeoutError:
            continue


if __name__ == "__main__":
    asyncio.run(run_loop())
