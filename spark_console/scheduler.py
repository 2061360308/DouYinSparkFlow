from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import SparkTask, TaskRun


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def compute_next_run(send_time: str, now_utc: datetime) -> datetime:
    hour, minute = map(int, send_time.split(":"))
    local_now = _utc(now_utc).astimezone(ZoneInfo("Asia/Shanghai"))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def claim_next_due_task(
    session: Session, now: datetime, worker_id: str
) -> TaskRun | None:
    now_utc = _utc(now)
    task = session.scalar(
        select(SparkTask)
        .where(
            SparkTask.enabled.is_(True),
            SparkTask.douyin_account_id.is_not(None),
            SparkTask.next_run_at.is_not(None),
            SparkTask.next_run_at <= now_utc,
        )
        .order_by(SparkTask.next_run_at, SparkTask.id)
        .limit(1)
    )
    if task is None:
        return None
    scheduled_for = _utc(task.next_run_at)
    run = TaskRun(
        task_id=task.id,
        scheduled_for=scheduled_for,
        status="running",
        stage="claimed",
        started_at=now_utc,
    )
    session.add(run)
    task.next_run_at = compute_next_run(task.send_time, now_utc)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return None
    return run


def finish_run(
    run: TaskRun,
    status: str,
    stage: str,
    now: datetime,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> TaskRun:
    run.status = status
    run.stage = stage
    run.finished_at = _utc(now)
    run.error_code = error_code
    run.error_summary = error_summary[:240] if error_summary else None
    return run
