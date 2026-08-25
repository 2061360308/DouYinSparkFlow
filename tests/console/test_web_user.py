import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from spark_console.config import Settings
from spark_console.db import create_schema, session_scope
from spark_console.models import User
from spark_console.security import PasswordService
from spark_console.web.app import create_app


class UserWebTests(unittest.TestCase):
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
        with session_scope(self.engine) as session:
            session.add(
                User(
                    username="friend",
                    password_hash=PasswordService().hash("Temporary-123!"),
                    role="user",
                    must_change_password=True,
                )
            )
        self.client = TestClient(
            create_app(self.settings, self.engine), base_url="https://testserver"
        )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def login(self):
        return self.client.post(
            "/login",
            data={"username": "friend", "password": "Temporary-123!"},
            follow_redirects=False,
        )

    def test_first_login_is_forced_to_change_password(self):
        response = self.login()
        self.assertEqual(303, response.status_code)
        self.assertEqual("/change-password", response.headers["location"])
        dashboard = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(303, dashboard.status_code)
        self.assertEqual("/change-password", dashboard.headers["location"])

    def test_post_without_csrf_is_rejected(self):
        self.login()
        response = self.client.post(
            "/change-password",
            data={"current_password": "Temporary-123!", "new_password": "Permanent-123!"},
        )
        self.assertEqual(403, response.status_code)

    def test_wrong_password_renders_visible_login_error(self):
        response = self.client.post(
            "/login",
            data={"username": "friend", "password": "definitely-wrong"},
            follow_redirects=False,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("用户名或密码错误", response.text)
        self.assertIn('name="password"', response.text)

    def test_http_mode_issues_a_session_cookie_the_browser_can_return(self):
        http_settings = replace(self.settings, secure_cookies=False)
        with TestClient(
            create_app(http_settings, self.engine), base_url="http://testserver"
        ) as client:
            response = client.post(
                "/login",
                data={"username": "friend", "password": "Temporary-123!"},
                follow_redirects=False,
            )
            self.assertNotIn("secure", response.headers["set-cookie"].lower())
            self.assertEqual(200, client.get("/change-password").status_code)

    def test_cookie_value_never_appears_after_account_save(self):
        self.login()
        page = self.client.get("/change-password")
        marker = 'name="csrf_token" value="'
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]
        self.client.post(
            "/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "Temporary-123!",
                "new_password": "Permanent-123!",
            },
        )
        page = self.client.get("/accounts")
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]
        secret = '[{"name":"sessionid","value":"sessionid-secret"}]'
        response = self.client.post(
            "/accounts",
            data={"csrf_token": csrf, "display_name": "主账号", "cookies": secret},
            follow_redirects=True,
        )
        self.assertEqual(200, response.status_code)
        self.assertNotIn("sessionid-secret", response.text)


if __name__ == "__main__":
    unittest.main()
