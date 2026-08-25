import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.models import AuditEvent, DouyinAccount, SparkTask, User
from spark_console.security import PasswordService
from spark_console.services import NotFound, ValidationError
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
from spark_console.services.tasks import TaskService
from spark_console.services.users import UserService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        create_schema(self.engine)
        self.session = Session(self.engine)
        self.audit = AuditService(self.session)
        self.users = UserService(self.session, PasswordService(), self.audit)
        self.accounts = AccountService(
            self.session, CookieCipher(b"c" * 32), self.audit
        )
        self.tasks = TaskService(self.session, self.accounts, self.audit)
        self.owner, _ = self.users.create("friend", "Temporary-123!", "user")
        self.other, _ = self.users.create("other", "Temporary-456!", "user")
        self.account = self.accounts.create(
            self.owner.id, "我的账号", b'[{"name":"sid","value":"secret"}]'
        )
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_user_cannot_read_or_delete_another_users_task(self):
        task = self.tasks.create(
            self.owner.id, self.account.id, "朋友", "09:00", "今日火花"
        )
        with self.assertRaises(NotFound):
            self.tasks.get_owned(self.other.id, task.id)
        with self.assertRaises(NotFound):
            self.tasks.delete_owned(self.other.id, task.id)

    def test_cookie_is_encrypted_and_never_returned_by_list(self):
        self.session.flush()
        stored = self.session.get(DouyinAccount, self.account.id)
        self.assertNotIn(b"secret", stored.encrypted_cookies)
        public = self.accounts.list_owned(self.owner.id)
        self.assertEqual([{"id": self.account.id, "display_name": "我的账号", "validation_state": "unknown"}], public)

    def test_deleting_account_erases_cookie_and_disables_tasks(self):
        task = self.tasks.create(
            self.owner.id, self.account.id, "朋友", "09:00", "今日火花"
        )
        self.session.flush()
        self.accounts.delete_owned(self.owner.id, self.account.id)
        self.session.flush()
        self.assertIsNone(self.session.get(DouyinAccount, self.account.id))
        retained = self.session.get(SparkTask, task.id)
        self.assertFalse(retained.enabled)
        self.assertIsNone(retained.douyin_account_id)

    def test_validation_rejects_bad_time_and_empty_message(self):
        with self.assertRaises(ValidationError):
            self.tasks.create(self.owner.id, self.account.id, "朋友", "25:00", "x")
        with self.assertRaises(ValidationError):
            self.tasks.create(self.owner.id, self.account.id, "朋友", "09:00", " ")

    def test_audit_records_never_contain_cookie_payload(self):
        events = self.session.scalars(select(AuditEvent)).all()
        self.assertTrue(events)
        serialized = " ".join((e.detail or "") + e.action for e in events)
        self.assertNotIn("secret", serialized)


if __name__ == "__main__":
    unittest.main()
