import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from spark_console.config import Settings
from spark_console.db import create_engine_for, create_schema, session_scope
from spark_console.models import (
    DouyinAccount,
    DouyinAccountIdentity,
    DouyinLoginSession,
    InviteCode,
    ScanStatus,
    SparkTask,
    User,
    utc_now,
)


class SettingsTests(unittest.TestCase):
    def test_requires_existing_32_byte_cookie_key_file(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            cookie_key = root_path / "cookie.key"
            session_key = root_path / "session.key"
            cookie_key.write_bytes(b"short")
            session_key.write_bytes(b"s" * 32)

            with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
                Settings.from_env(
                    {
                        "SPARK_DATA_DIR": root,
                        "SPARK_COOKIE_KEY_FILE": str(cookie_key),
                        "SPARK_SESSION_KEY_FILE": str(session_key),
                    }
                )

    def test_loads_safe_defaults_from_valid_key_files(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            cookie_key = root_path / "cookie.key"
            session_key = root_path / "session.key"
            cookie_key.write_bytes(b"c" * 32)
            session_key.write_bytes(b"s" * 32)

            settings = Settings.from_env(
                {
                    "SPARK_DATA_DIR": root,
                    "SPARK_COOKIE_KEY_FILE": str(cookie_key),
                    "SPARK_SESSION_KEY_FILE": str(session_key),
                }
            )

            self.assertEqual("Asia/Shanghai", settings.timezone)
            self.assertEqual("127.0.0.1", settings.web_bind)
            self.assertEqual(8899, settings.web_port)
            self.assertTrue(settings.secure_cookies)
            self.assertTrue(settings.database_url.endswith("spark.db"))

    def test_can_explicitly_allow_http_session_cookie_for_temporary_deployment(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            cookie_key = root_path / "cookie.key"
            session_key = root_path / "session.key"
            cookie_key.write_bytes(b"c" * 32)
            session_key.write_bytes(b"s" * 32)
            settings = Settings.from_env(
                {
                    "SPARK_DATA_DIR": root,
                    "SPARK_COOKIE_KEY_FILE": str(cookie_key),
                    "SPARK_SESSION_KEY_FILE": str(session_key),
                    "SPARK_SECURE_COOKIES": "false",
                }
            )
            self.assertFalse(settings.secure_cookies)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / "cookie.key").write_bytes(b"c" * 32)
        (root / "session.key").write_bytes(b"s" * 32)
        self.settings = Settings.from_env(
            {
                "SPARK_DATA_DIR": str(root),
                "SPARK_COOKIE_KEY_FILE": str(root / "cookie.key"),
                "SPARK_SESSION_KEY_FILE": str(root / "session.key"),
            }
        )
        self.engine = create_engine_for(self.settings)
        create_schema(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_schema_enforces_unique_enabled_schedule(self):
        with session_scope(self.engine) as session:
            user = User(username="friend", password_hash="hash", role="user")
            session.add(user)
            session.flush()
            account = DouyinAccount(
                owner_user_id=user.id,
                display_name="main",
                encrypted_cookies=b"ciphertext",
                cookie_nonce=b"n" * 12,
            )
            session.add(account)
            session.flush()
            session.add(
                SparkTask(
                    owner_user_id=user.id,
                    douyin_account_id=account.id,
                    target_name="目标",
                    send_time="09:00",
                    message_template="今日火花",
                    enabled=True,
                )
            )
            session.flush()
            session.add(
                SparkTask(
                    owner_user_id=user.id,
                    douyin_account_id=account.id,
                    target_name="目标",
                    send_time="09:00",
                    message_template="今日火花",
                    enabled=True,
                )
            )
            with self.assertRaises(IntegrityError):
                session.flush()
            session.rollback()

    def test_schema_allows_only_one_global_scan_slot(self):
        with session_scope(self.engine) as session:
            owner = User(username="owner", password_hash="hash", role="user")
            session.add(owner)
            session.flush()
            session.add(
                DouyinLoginSession(
                    owner_user_id=owner.id,
                    slot="global",
                    status=ScanStatus.QUEUED,
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            )

        with self.assertRaises(IntegrityError):
            with session_scope(self.engine) as session:
                session.add(
                    DouyinLoginSession(
                        owner_user_id=owner.id,
                        slot="global",
                        status=ScanStatus.QUEUED,
                        expires_at=utc_now() + timedelta(minutes=5),
                    )
                )

        with session_scope(self.engine) as session:
            session.add(
                DouyinLoginSession(
                    owner_user_id=owner.id,
                    slot=None,
                    status=ScanStatus.SUCCEEDED,
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            )
            session.add(
                DouyinLoginSession(
                    owner_user_id=owner.id,
                    slot=None,
                    status=ScanStatus.EXPIRED,
                    expires_at=utc_now() + timedelta(minutes=5),
                )
            )

    def test_invite_model_has_no_plaintext_code_column(self):
        self.assertIn("code_hash", InviteCode.__table__.columns)
        self.assertNotIn("code", InviteCode.__table__.columns)

    def test_invite_persists_creator_and_consumer_foreign_keys(self):
        with session_scope(self.engine) as session:
            creator = User(username="creator", password_hash="hash", role="admin")
            consumer = User(username="consumer", password_hash="hash", role="user")
            session.add_all([creator, consumer])
            session.flush()
            invite = InviteCode(
                code_hash="a" * 64,
                created_by_user_id=creator.id,
                expires_at=utc_now() + timedelta(days=1),
                used_by_user_id=consumer.id,
                used_at=utc_now(),
            )
            session.add(invite)
            session.flush()
            self.assertEqual(creator.id, invite.created_by_user_id)
            self.assertEqual(consumer.id, invite.used_by_user_id)

        foreign_keys = {
            (foreign_key.parent.name, foreign_key.target_fullname)
            for foreign_key in InviteCode.__table__.foreign_keys
        }
        self.assertEqual(
            {
                ("created_by_user_id", "users.id"),
                ("used_by_user_id", "users.id"),
            },
            foreign_keys,
        )

    def test_douyin_identity_metadata_uses_one_to_one_table(self):
        self.assertEqual("douyin_account_identities", DouyinAccountIdentity.__tablename__)
        self.assertEqual(
            {"account_id"},
            {column.name for column in DouyinAccountIdentity.__table__.primary_key.columns},
        )
        self.assertNotIn("douyin_unique_id", DouyinAccount.__table__.columns)
        self.assertIn("douyin_unique_id", DouyinAccountIdentity.__table__.columns)


if __name__ == "__main__":
    unittest.main()
