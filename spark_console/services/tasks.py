from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spark_console.models import DouyinContactIdentity, SparkTask, SparkTaskTargetIdentity
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService


_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class TaskService:
    def __init__(self, session: Session, accounts: AccountService, audit: AuditService):
        self.session = session
        self.accounts = accounts
        self.audit = audit

    def get_owned(self, owner_id: str, task_id: str) -> SparkTask:
        task = self.session.scalar(
            select(SparkTask).where(
                SparkTask.id == task_id, SparkTask.owner_user_id == owner_id
            )
        )
        if task is None:
            raise NotFound("task not found")
        return task

    def list_owned(self, owner_id: str) -> list[SparkTask]:
        return list(
            self.session.scalars(
                select(SparkTask)
                .where(SparkTask.owner_user_id == owner_id)
                .order_by(SparkTask.send_time, SparkTask.created_at)
            ).all()
        )

    def create(
        self,
        owner_id: str,
        account_id: str,
        target_name: str,
        send_time: str,
        message_template: str,
        target_sec_uid: str | None = None,
    ) -> SparkTask:
        target = target_name.strip()
        message = message_template.strip()
        if not target or len(target) > 64:
            raise ValidationError("好友名称须为 1–64 个字符")
        if not _TIME_RE.fullmatch(send_time):
            raise ValidationError("发送时间格式必须为 HH:MM")
        if not message or len(message) > 500:
            raise ValidationError("消息内容须为 1–500 个字符")
        account = self.accounts.get_owned(owner_id, account_id)
        stable_target = str(target_sec_uid or "").strip()
        if stable_target and self.session.get(
            DouyinContactIdentity, (account.id, stable_target)
        ) is None:
            raise ValidationError("所选好友不属于当前抖音账号")
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour, minute = map(int, send_time.split(":"))
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            from datetime import timedelta
            candidate += timedelta(days=1)
        task = SparkTask(
            owner_user_id=owner_id,
            douyin_account_id=account.id,
            target_name=target,
            send_time=send_time,
            message_template=message,
            enabled=True,
            next_run_at=candidate.astimezone(timezone.utc),
        )
        self.session.add(task)
        try:
            self.session.flush()
            if stable_target:
                self.session.add(
                    SparkTaskTargetIdentity(
                        task_id=task.id,
                        sec_uid=stable_target,
                    )
                )
                self.session.flush()
        except IntegrityError as error:
            self.session.rollback()
            raise Conflict("相同账号、好友和时间的启用任务已存在") from error
        self.audit.write(owner_id, "task.created", "spark_task", task.id)
        return task

    def set_enabled_owned(self, owner_id: str, task_id: str, enabled: bool) -> SparkTask:
        task = self.get_owned(owner_id, task_id)
        if enabled and task.douyin_account_id is None:
            raise ValidationError("账号已删除，无法启用任务")
        task.enabled = enabled
        self.audit.write(owner_id, "task.enabled" if enabled else "task.disabled", "spark_task", task.id)
        return task

    def delete_owned(self, owner_id: str, task_id: str) -> None:
        task = self.get_owned(owner_id, task_id)
        self.session.delete(task)
        self.audit.write(owner_id, "task.deleted", "spark_task", task_id)
