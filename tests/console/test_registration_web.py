import hashlib
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.config import Settings
from spark_console.db import create_schema, session_scope
from spark_console.models import InviteCode, User
from spark_console.security import PasswordService
from spark_console.services.audits import AuditService
from spark_console.services.invites import InviteService
from spark_console.web.app import create_app
from spark_console.web.registration_routes import admin_invite_items, registration_client_key


PUBLIC_ERROR = "注册信息或邀请码无效"


class RegistrationWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            data_dir=root,
            database_url=f"sqlite:///{root / 'test.db'}",
            cookie_key_file=root / "cookie.key",
            session_key_file=root / "session.key",
        )
        self.settings.cookie_key_file.write_bytes(b"c" * 32)
        self.settings.session_key_file.write_bytes(b"s" * 32)
        self.engine = create_engine(
            self.settings.database_url, connect_args={"check_same_thread": False}
        )
        create_schema(self.engine)
        passwords = PasswordService()
        with session_scope(self.engine) as session:
            self.admin = User(
                username="admin",
                password_hash=passwords.hash("AdminPass123"),
                role="admin",
                must_change_password=False,
            )
            self.friend = User(
                username="friend",
                password_hash=passwords.hash("FriendPass123"),
                role="user",
                must_change_password=False,
            )
            session.add_all([self.admin, self.friend])
            session.flush()
            invite, self.invite_plaintext = InviteService(
                session, AuditService(session)
            ).create(self.admin.id)
            self.unused_invite_id = invite.id
        self.client = TestClient(
            create_app(self.settings, self.engine), base_url="https://testserver"
        )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def login(self, username="admin", password="AdminPass123"):
        return self.client.post(
            "/login", data={"username": username, "password": password}, follow_redirects=False
        )

    def csrf_for(self, path="/admin"):
        response = self.client.get(path)
        self.assertEqual(200, response.status_code)
        marker = 'name="csrf_token" value="'
        return response.text.split(marker, 1)[1].split('"', 1)[0]

    def registration_data(self, username="newfriend", invite_code=None, **overrides):
        data = {
            "username": username,
            "password": "StrongPass10",
            "password_confirmation": "StrongPass10",
            "invite_code": self.invite_plaintext if invite_code is None else invite_code,
        }
        data.update(overrides)
        return data

    def test_login_page_links_to_registration(self):
        response = self.client.get("/login")

        self.assertEqual(200, response.status_code)
        self.assertIn('href="/register"', response.text)

    def test_valid_invite_registers_ordinary_user_then_redirects_to_login(self):
        response = self.client.post(
            "/register", data=self.registration_data(), follow_redirects=False
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/login?registered=1", response.headers["location"])
        with Session(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "newfriend"))
            self.assertEqual("user", user.role)
            self.assertFalse(user.must_change_password)

    def test_password_mismatch_returns_public_error_without_creating_user(self):
        response = self.client.post(
            "/register",
            data=self.registration_data(password_confirmation="DifferentPass10"),
            follow_redirects=False,
        )

        self.assertEqual(400, response.status_code)
        self.assertIn(PUBLIC_ERROR, response.text)
        with Session(self.engine) as session:
            self.assertIsNone(session.scalar(select(User).where(User.username == "newfriend")))

    def test_invalid_and_used_invites_have_identical_public_error(self):
        invalid = self.client.post(
            "/register", data=self.registration_data(invite_code="not-an-invite"), follow_redirects=False
        )
        used = self.client.post(
            "/register", data=self.registration_data(username="first"), follow_redirects=False
        )
        reused = self.client.post(
            "/register", data=self.registration_data(username="second"), follow_redirects=False
        )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual(303, used.status_code)
        self.assertEqual(400, reused.status_code)
        self.assertIn(PUBLIC_ERROR, invalid.text)
        self.assertIn(PUBLIC_ERROR, reused.text)
        self.assertEqual(invalid.text, reused.text)

    def test_registration_failures_are_rate_limited_by_client_address(self):
        for index in range(10):
            response = self.client.post(
                "/register",
                data=self.registration_data(username=f"bad{index}", invite_code="invalid"),
                follow_redirects=False,
            )
            self.assertEqual(400, response.status_code)

        limited = self.client.post(
            "/register", data=self.registration_data(username="blocked"), follow_redirects=False
        )

        self.assertEqual(429, limited.status_code)
        self.assertIn(PUBLIC_ERROR, limited.text)

    def test_missing_registration_fields_use_the_public_error_and_failure_limiter(self):
        complete = self.registration_data()
        missing_fields = [
            {key: value for key, value in complete.items() if key != field}
            for field in complete
        ]
        for index, data in enumerate(missing_fields + [missing_fields[-1]] * 6):
            response = self.client.post("/register", data=data, follow_redirects=False)
            self.assertEqual(400, response.status_code, f"missing field attempt {index}")
            self.assertIn(PUBLIC_ERROR, response.text)

        limited = self.client.post(
            "/register", data=self.registration_data(username="still-blocked"), follow_redirects=False
        )

        self.assertEqual(429, limited.status_code)
        self.assertIn(PUBLIC_ERROR, limited.text)

    def test_registration_client_key_uses_valid_real_ip_and_falls_back_on_invalid_value(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/register",
                "headers": [(b"x-real-ip", b"203.0.113.7")],
                "client": ("127.0.0.1", 6000),
            }
        )
        invalid = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/register",
                "headers": [(b"x-real-ip", b"not-an-ip")],
                "client": ("127.0.0.1", 6000),
            }
        )

        self.assertEqual("203.0.113.7", registration_client_key(request))
        self.assertEqual("127.0.0.1", registration_client_key(invalid))

    def test_register_page_accurately_says_registration_returns_to_login(self):
        response = self.client.get("/register")

        self.assertEqual(200, response.status_code)
        self.assertIn("注册并返回登录", response.text)
        self.assertNotIn("注册并登录", response.text)

    def test_invite_generation_is_admin_only_and_displays_code_once(self):
        self.login("friend", "FriendPass123")
        denied = self.client.post("/admin/invites", data={"csrf_token": "anything"})
        self.assertEqual(404, denied.status_code)

        self.login()
        csrf = self.csrf_for()
        generated = self.client.post("/admin/invites", data={"csrf_token": csrf})

        self.assertEqual(200, generated.status_code)
        code = re.search(r"<code>([^<]+)</code>", generated.text).group(1)
        self.assertGreaterEqual(len(code), 24)
        self.assertIn(code, generated.text)
        refreshed = self.client.get("/admin")
        self.assertNotIn(code, refreshed.text)
        with Session(self.engine) as session:
            self.assertIsNotNone(
                session.scalar(
                    select(InviteCode).where(
                        InviteCode.code_hash == hashlib.sha256(code.encode()).hexdigest()
                    )
                )
            )

    def test_invite_revoke_requires_csrf(self):
        with session_scope(self.engine) as session:
            invite, _code = InviteService(session, AuditService(session)).create(self.admin.id)
            self.invite_id = invite.id
        self.login()

        response = self.client.post(f"/admin/invites/{self.invite_id}/revoke", data={})

        self.assertEqual(403, response.status_code)
        with Session(self.engine) as session:
            self.assertIsNone(session.get(InviteCode, self.invite_id).revoked_at)

    def test_admin_can_revoke_an_unused_invite(self):
        with session_scope(self.engine) as session:
            invite, _code = InviteService(session, AuditService(session)).create(self.admin.id)
            invite_id = invite.id
        self.login()

        response = self.client.post(
            f"/admin/invites/{invite_id}/revoke",
            data={"csrf_token": self.csrf_for()},
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/admin", response.headers["location"])
        with Session(self.engine) as session:
            self.assertIsNotNone(session.get(InviteCode, invite_id).revoked_at)

    def test_admin_invite_list_projects_lifecycle_and_consumer_details(self):
        now = datetime.now(timezone.utc)
        with session_scope(self.engine) as session:
            expired = InviteCode(
                code_hash=hashlib.sha256(b"expired").hexdigest(),
                created_by_user_id=self.admin.id,
                expires_at=now - timedelta(minutes=1),
            )
            used = InviteCode(
                code_hash=hashlib.sha256(b"used").hexdigest(),
                created_by_user_id=self.admin.id,
                expires_at=now + timedelta(days=1),
                used_by_user_id=self.friend.id,
                used_at=now,
            )
            revoked = InviteCode(
                code_hash=hashlib.sha256(b"revoked").hexdigest(),
                created_by_user_id=self.admin.id,
                expires_at=now + timedelta(days=1),
                revoked_at=now,
            )
            session.add_all([expired, used, revoked])
            session.flush()
            expired_id, used_id, revoked_id = expired.id, used.id, revoked.id
        self.login()

        response = self.client.get("/admin")

        self.assertEqual(200, response.status_code)
        self.assertIn("有效", response.text)
        self.assertIn("已过期", response.text)
        self.assertIn("已使用", response.text)
        self.assertIn("已撤销", response.text)
        self.assertIn("有效期至", response.text)
        self.assertIn("使用者：friend", response.text)
        self.assertIn(f"/admin/invites/{self.unused_invite_id}/revoke", response.text)
        for invite_id in (expired_id, used_id, revoked_id):
            self.assertNotIn(f"/admin/invites/{invite_id}/revoke", response.text)
        with Session(self.engine) as session:
            items = {item.invite.id: item for item in admin_invite_items(session)}
        self.assertEqual("有效", items[self.unused_invite_id].status)
        self.assertTrue(items[self.unused_invite_id].can_revoke)
        self.assertEqual("已过期", items[expired_id].status)
        self.assertFalse(items[expired_id].can_revoke)
        self.assertEqual("已使用", items[used_id].status)
        self.assertEqual("friend", items[used_id].used_by_username)
        self.assertEqual("已撤销", items[revoked_id].status)


if __name__ == "__main__":
    unittest.main()
