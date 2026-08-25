import hashlib
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.db import create_schema
from spark_console.models import AuditEvent, InviteCode
from spark_console.rate_limit import FailedAttemptLimiter
from spark_console.security import PasswordService
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.services.users import UserService, validate_registration_password


class InviteServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        self.engine = create_engine("sqlite:///:memory:")
        create_schema(self.engine)
        self.session = Session(self.engine)
        self.audit = AuditService(self.session)
        self.users = UserService(self.session, PasswordService(), self.audit)
        self.admin, _ = self.users.create("admin", "Temporary-123!", "admin")
        self.user, _ = self.users.create("friend", "Temporary-123!", "user")
        self.other, _ = self.users.create("other", "Temporary-123!", "user")
        self.invites = InviteService(self.session, self.audit, now=lambda: self.now)
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_invite_plaintext_is_returned_once_and_only_digest_is_stored(self):
        invite, plaintext = self.invites.create(self.admin.id)

        self.assertGreaterEqual(len(plaintext), 24)
        self.assertNotEqual(plaintext, invite.code_hash)
        self.assertEqual(hashlib.sha256(plaintext.encode()).hexdigest(), invite.code_hash)
        self.assertEqual(self.now + timedelta(days=7), invite.expires_at)
        self.assertNotIn(
            plaintext,
            " ".join(e.detail or "" for e in self.session.scalars(select(AuditEvent))),
        )

    def test_invite_can_be_consumed_only_once(self):
        invite, plaintext = self.invites.create(self.admin.id)

        self.invites.consume(plaintext, self.user.id)

        self.assertEqual(self.user.id, invite.used_by_user_id)
        self.assertEqual(self.now, invite.used_at)
        with self.assertRaisesRegex(ValidationError, "注册信息或邀请码无效"):
            self.invites.consume(plaintext, self.other.id)

    def test_expired_invite_is_rejected(self):
        _invite, plaintext = self.invites.create(self.admin.id, timedelta(minutes=1))
        self.now += timedelta(minutes=1)

        with self.assertRaisesRegex(ValidationError, "注册信息或邀请码无效"):
            self.invites.consume(plaintext, self.user.id)

    def test_revoke_prevents_consumption(self):
        invite, plaintext = self.invites.create(self.admin.id)

        self.invites.revoke(self.admin.id, invite.id)

        self.assertEqual(self.now, invite.revoked_at.replace(tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValidationError, "注册信息或邀请码无效"):
            self.invites.consume(plaintext, self.user.id)

    def test_list_all_returns_newest_invites_first(self):
        older, _ = self.invites.create(self.admin.id)
        self.now += timedelta(seconds=1)
        newer, _ = self.invites.create(self.admin.id)

        self.assertEqual([newer, older], self.invites.list_all())


class RegistrationPasswordTests(unittest.TestCase):
    def test_registration_password_requires_ten_characters_letters_and_digits(self):
        validate_registration_password("StrongPass1")
        for password in ("Short1234", "abcdefghij", "1234567890"):
            with self.subTest(password=password):
                with self.assertRaisesRegex(ValidationError, "注册信息或邀请码无效"):
                    validate_registration_password(password)


class FailedAttemptLimiterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 25, 6, 0, tzinfo=timezone.utc)
        self.limiter = FailedAttemptLimiter(
            limit=2,
            window=timedelta(minutes=10),
            now=lambda: self.now,
        )

    def test_failures_are_limited_then_expire_from_the_sliding_window(self):
        self.assertTrue(self.limiter.allow("127.0.0.1"))
        self.limiter.record_failure("127.0.0.1")
        self.limiter.record_failure("127.0.0.1")

        self.assertFalse(self.limiter.allow("127.0.0.1"))
        self.now += timedelta(minutes=10)
        self.assertTrue(self.limiter.allow("127.0.0.1"))

    def test_clear_removes_only_the_key_failures(self):
        self.limiter.record_failure("one")
        self.limiter.record_failure("two")

        self.limiter.clear("one")

        self.assertNotIn("one", self.limiter.attempts)
        self.assertIn("two", self.limiter.attempts)


if __name__ == "__main__":
    unittest.main()
