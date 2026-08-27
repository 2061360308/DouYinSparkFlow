import hashlib
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.web_chat import DouyinUserIdentity
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.models import (
    AuditEvent,
    DouyinAccount,
    DouyinAccountIdentity,
    DouyinContactIdentity,
    SparkTask,
    SparkTaskTargetIdentity,
    User,
)
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
        cookie_marker = b"secret"
        self.assertFalse(cookie_marker in stored.encrypted_cookies)
        public = self.accounts.list_owned(self.owner.id)
        self.assertEqual(1, len(public))
        self.assertEqual({"id", "display_name", "validation_state"}, set(public[0]))
        self.assertEqual(self.account.id, public[0]["id"])
        self.assertEqual("我的账号", public[0]["display_name"])
        self.assertEqual("unknown", public[0]["validation_state"])

    def test_storage_state_is_encrypted_with_identity_and_safe_projection(self):
        state = {
            "cookies": [
                {
                    "name": "sid",
                    "value": "storage-cookie-marker",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [
                {
                    "origin": "https://www.douyin.com",
                    "localStorage": [
                        {"name": "session", "value": "local-storage-marker"}
                    ],
                }
            ],
        }

        account = self.accounts.create_from_storage_state(
            self.owner.id, " 扫码账号 ", state, " douyin-123 "
        )
        self.session.flush()

        stored = self.session.get(DouyinAccount, account.id)
        identity = self.session.get(DouyinAccountIdentity, account.id)
        encrypted = stored.encrypted_cookies
        cookie_marker = b"storage-cookie-marker"
        storage_marker = b"local-storage-marker"
        self.assertFalse(cookie_marker in encrypted)
        self.assertFalse(storage_marker in encrypted)
        self.assertEqual(2, stored.cookie_version)
        self.assertEqual("valid", stored.validation_state)
        self.assertIsNotNone(stored.last_verified_at)
        self.assertEqual("douyin-123", identity.douyin_unique_id)
        plaintext = self.accounts.cipher.decrypt(
            stored.encrypted_cookies, stored.cookie_nonce
        )
        self.assertEqual(
            "2275bd33a9235dd8a676e0a62ce3b965a24e5a28054245ec6e85c2c65717ffd1",
            hashlib.sha256(plaintext).hexdigest(),
        )
        projected = next(
            item for item in self.accounts.list_owned(self.owner.id) if item["id"] == account.id
        )
        self.assertEqual(
            {"id", "display_name", "validation_state"}, set(projected)
        )

        audits = " ".join(
            f"{event.action} {event.detail or ''}"
            for event in self.session.scalars(select(AuditEvent)).all()
        )
        self.assertFalse(cookie_marker.decode() in audits)
        self.assertFalse(storage_marker.decode() in audits)

    def test_stable_contact_identity_is_saved_and_bound_to_new_task(self):
        state = {
            "cookies": [
                {
                    "name": "sid",
                    "value": "secret",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }
        account = self.accounts.create_from_storage_state(
            self.owner.id,
            "稳定账号",
            state,
            contact_identities=(
                DouyinUserIdentity(
                    sec_uid="stable-user-id",
                    short_id="123456",
                    unique_id="search-id",
                    nickname="新的昵称",
                    remark_name="我的备注",
                ),
            ),
        )

        task = self.tasks.create(
            self.owner.id,
            account.id,
            "我的备注",
            "09:00",
            "今日火花",
            target_sec_uid="stable-user-id",
        )
        self.session.flush()

        contact = self.session.get(
            DouyinContactIdentity, (account.id, "stable-user-id")
        )
        binding = self.session.get(SparkTaskTargetIdentity, task.id)
        self.assertEqual("新的昵称", contact.nickname)
        self.assertEqual("我的备注", contact.remark_name)
        self.assertEqual("stable-user-id", binding.sec_uid)

    def test_task_rejects_contact_identity_owned_by_another_account(self):
        other_account = self.accounts.create(
            self.owner.id, "其他账号", b'[{"name":"sid","value":"other"}]'
        )
        self.session.add(
            DouyinContactIdentity(
                account_id=other_account.id,
                sec_uid="other-account-contact",
                nickname="不属于当前账号",
            )
        )
        self.session.flush()

        with self.assertRaises(ValidationError):
            self.tasks.create(
                self.owner.id,
                self.account.id,
                "不属于当前账号",
                "09:00",
                "今日火花",
                target_sec_uid="other-account-contact",
            )

    def test_storage_state_rejects_empty_cookies_before_creating_account(self):
        before = len(self.session.scalars(select(DouyinAccount)).all())

        with self.assertRaises(ValidationError):
            self.accounts.create_from_storage_state(
                self.owner.id,
                "扫码账号",
                {"cookies": [], "origins": []},
            )

        self.assertEqual(before, len(self.session.scalars(select(DouyinAccount)).all()))

    def test_storage_state_rejects_playwright_invalid_domain_before_encryption(self):
        before = len(self.session.scalars(select(DouyinAccount)).all())
        invalid_domains = (
            "\u0301a.com",
            "foo%3Abar",
            "@",
            "#",
            "?",
            "999.999.999",
            "256.1.1.1.",
        )

        with patch.object(
            self.accounts.cipher,
            "encrypt",
            side_effect=AssertionError("encryption boundary crossed"),
        ):
            for case, domain in enumerate(invalid_domains):
                with self.subTest(case=case):
                    state = {
                        "cookies": [
                            {
                                "name": "probe",
                                "value": "x",
                                "domain": domain,
                                "path": "/",
                                "expires": -1,
                                "httpOnly": True,
                                "secure": True,
                                "sameSite": "Lax",
                            }
                        ],
                        "origins": [],
                    }
                    with self.assertRaises(ValidationError):
                        self.accounts.create_from_storage_state(
                            self.owner.id, "扫码账号", state
                        )

        after = len(self.session.scalars(select(DouyinAccount)).all())
        self.assertEqual(before, after)

    def test_owner_can_rename_account_but_another_user_cannot(self):
        renamed = self.accounts.rename_owned(
            self.owner.id, self.account.id, " 生活号 "
        )

        self.assertEqual("生活号", renamed.display_name)
        rename_events = self.session.scalars(
            select(AuditEvent).where(AuditEvent.action == "account.renamed")
        ).all()
        self.assertEqual(1, len(rename_events))
        self.assertTrue(all(event.detail is None for event in rename_events))
        with self.assertRaises(NotFound):
            self.accounts.rename_owned(self.other.id, self.account.id, "越权名称")
        with self.assertRaises(ValidationError):
            self.accounts.rename_owned(self.owner.id, self.account.id, " ")

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
        cookie_marker = "secret"
        self.assertFalse(cookie_marker in serialized)


if __name__ == "__main__":
    unittest.main()
