import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from spark_console.db import create_schema
from spark_console.models import DouyinAccount, SparkTask, User
from spark_console.scheduler import claim_next_due_task, compute_next_run


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


if __name__ == "__main__":
    unittest.main()
