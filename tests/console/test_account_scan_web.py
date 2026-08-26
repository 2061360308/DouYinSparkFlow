import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema, session_scope
from spark_console.models import DouyinAccount, DouyinLoginSession, User
from spark_console.security import PasswordService
from spark_console.services.scan_sessions import PNG_SIGNATURE, ScanSessionService
from spark_console.web.app import create_app


class AccountScanWebTests(unittest.TestCase):
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
        password_hash = PasswordService().hash("Permanent-123!")
        with session_scope(self.engine) as db:
            owner = User(
                username="owner",
                password_hash=password_hash,
                role="user",
                must_change_password=False,
            )
            other = User(
                username="other",
                password_hash=password_hash,
                role="user",
                must_change_password=False,
            )
            admin = User(
                username="admin",
                password_hash=password_hash,
                role="admin",
                must_change_password=False,
            )
            db.add_all([owner, other, admin])
            db.flush()
            self.owner_id = owner.id
            self.other_id = other.id
            self.admin_id = admin.id
            account = DouyinAccount(
                owner_user_id=owner.id,
                display_name="旧备注",
                encrypted_cookies=b"encrypted",
                cookie_nonce=b"nonce",
                validation_state="valid",
            )
            db.add(account)
            db.flush()
            self.account_id = account.id

        app = create_app(self.settings, self.engine)
        self.owner_client = TestClient(app, base_url="https://testserver")
        self.other_client = TestClient(app, base_url="https://testserver")
        self.admin_client = TestClient(app, base_url="https://testserver")
        self._login(self.owner_client, "owner")
        self._login(self.other_client, "other")
        self._login(self.admin_client, "admin")
        self.owner_csrf = self._csrf(self.owner_client)
        self.other_csrf = self._csrf(self.other_client)
        self.admin_csrf = self._csrf(self.admin_client)

    def tearDown(self):
        self.owner_client.close()
        self.other_client.close()
        self.admin_client.close()
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _login(client, username):
        response = client.post(
            "/login",
            data={"username": username, "password": "Permanent-123!"},
            follow_redirects=False,
        )
        if response.status_code != 303:
            raise AssertionError(response.text)

    @staticmethod
    def _csrf(client):
        page = client.get("/accounts")
        marker = (
            'data-csrf-token="'
            if 'data-csrf-token="' in page.text
            else 'name="csrf_token" value="'
        )
        return page.text.split(marker, 1)[1].split('"', 1)[0]

    def _awaiting_scan(self):
        with session_scope(self.engine) as db:
            service = ScanSessionService(db)
            scan = service.start(self.owner_id)
            scan_id = scan.id
            service.claim_next()
            service.publish_qr(scan_id, PNG_SIGNATURE + b"fixture")
        return scan_id

    def test_authenticated_owner_can_start_with_csrf_and_client_fields_are_ignored(self):
        response = self.owner_client.post(
            "/accounts/scan",
            data={
                "csrf_token": self.owner_csrf,
                "owner_id": self.other_id,
                "status": "succeeded",
                "display_name": "browser supplied",
                "cookies": "credential-marker",
            },
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(
            {
                "id",
                "status",
                "remaining_seconds",
                "error",
                "message",
                "account_id",
            },
            set(response.json()),
        )
        with session_scope(self.engine) as db:
            scan = db.scalar(select(DouyinLoginSession))
            self.assertEqual(self.owner_id, scan.owner_user_id)
            self.assertEqual("queued", scan.status)
            self.assertEqual(1, len(db.scalars(select(DouyinAccount)).all()))

    def test_start_requires_login_and_csrf_and_busy_is_a_stable_json_conflict(self):
        anonymous = TestClient(
            create_app(self.settings, self.engine), base_url="https://testserver"
        )
        try:
            redirected = anonymous.post(
                "/accounts/scan", data={"csrf_token": "unused"}, follow_redirects=False
            )
        finally:
            anonymous.close()
        self.assertEqual(303, redirected.status_code)
        self.assertEqual("/login", redirected.headers["location"])

        missing_csrf = self.owner_client.post("/accounts/scan")
        self.assertEqual(403, missing_csrf.status_code)

        created = self.owner_client.post(
            "/accounts/scan", data={"csrf_token": self.owner_csrf}
        )
        self.assertEqual(201, created.status_code)
        busy = self.other_client.post(
            "/accounts/scan", data={"csrf_token": self.other_csrf}
        )
        self.assertEqual(409, busy.status_code)
        self.assertEqual(
            {
                "error": "slot_busy",
                "message": "扫码通道正在使用，请稍后重试",
            },
            busy.json(),
        )

    def test_same_owner_resumes_active_scan_instead_of_reporting_busy(self):
        created = self.owner_client.post(
            "/accounts/scan", data={"csrf_token": self.owner_csrf}
        )

        resumed = self.owner_client.post(
            "/accounts/scan", data={"csrf_token": self.owner_csrf}
        )

        self.assertEqual(201, created.status_code)
        self.assertEqual(200, resumed.status_code)
        self.assertEqual(created.json()["id"], resumed.json()["id"])

    def test_start_requests_are_rate_limited_with_a_stable_error(self):
        responses = [
            self.owner_client.post(
                "/accounts/scan", data={"csrf_token": self.owner_csrf}
            )
            for _ in range(6)
        ]

        self.assertEqual([201, 200, 200, 200, 200, 429], [r.status_code for r in responses])
        self.assertEqual(
            {"error": "rate_limited", "message": "请求过于频繁，请稍后重试"},
            responses[-1].json(),
        )

    def test_polling_and_qr_requests_have_independent_safe_rate_limits(self):
        scan_id = self._awaiting_scan()

        status_responses = [
            self.owner_client.get(f"/accounts/scan/{scan_id}") for _ in range(41)
        ]
        qr_responses = [
            self.owner_client.get(f"/accounts/scan/{scan_id}/qr") for _ in range(41)
        ]

        self.assertTrue(all(response.status_code == 200 for response in status_responses[:40]))
        self.assertTrue(all(response.status_code == 200 for response in qr_responses[:40]))
        for response in (status_responses[-1], qr_responses[-1]):
            self.assertEqual(429, response.status_code)
            self.assertEqual(
                {"error": "rate_limited", "message": "请求过于频繁，请稍后重试"},
                response.json(),
            )

    def test_cancel_requests_are_rate_limited_independently(self):
        responses = []
        for _ in range(11):
            with session_scope(self.engine) as db:
                scan_id = ScanSessionService(db).start(self.owner_id).id
            responses.append(
                self.owner_client.post(
                    f"/accounts/scan/{scan_id}/cancel",
                    data={"csrf_token": self.owner_csrf},
                )
            )

        self.assertTrue(all(response.status_code == 200 for response in responses[:10]))
        self.assertEqual(429, responses[-1].status_code)
        self.assertEqual("rate_limited", responses[-1].json()["error"])

    def test_status_is_owner_only_no_store_and_exactly_public_projection(self):
        scan_id = self._awaiting_scan()

        response = self.owner_client.get(f"/accounts/scan/{scan_id}")

        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(
            {
                "id",
                "status",
                "remaining_seconds",
                "error",
                "message",
                "account_id",
            },
            set(response.json()),
        )
        self.assertEqual("awaiting_scan", response.json()["status"])
        self.assertNotIn("qr_png", response.text)
        self.assertNotIn(self.owner_id, response.text)
        for client in (self.other_client, self.admin_client):
            hidden = client.get(f"/accounts/scan/{scan_id}")
            self.assertEqual(404, hidden.status_code)
            self.assertEqual(
                {"error": "not_found", "message": "未找到扫码会话"},
                hidden.json(),
            )

    def test_owner_can_fetch_no_store_qr_but_other_user_and_admin_get_404(self):
        scan_id = self._awaiting_scan()

        response = self.owner_client.get(f"/accounts/scan/{scan_id}/qr")

        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.headers["content-type"])
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(PNG_SIGNATURE + b"fixture", response.content)
        self.assertEqual(
            404, self.other_client.get(f"/accounts/scan/{scan_id}/qr").status_code
        )
        self.assertEqual(
            404, self.admin_client.get(f"/accounts/scan/{scan_id}/qr").status_code
        )

    def test_owner_can_send_normalized_browser_click_with_csrf(self):
        scan_id = self._awaiting_scan()

        missing_csrf = self.owner_client.post(
            f"/accounts/scan/{scan_id}/interact", data={"x": "0.25", "y": "0.75"}
        )
        hidden = self.other_client.post(
            f"/accounts/scan/{scan_id}/interact",
            data={"csrf_token": self.other_csrf, "x": "0.25", "y": "0.75"},
        )
        accepted = self.owner_client.post(
            f"/accounts/scan/{scan_id}/interact",
            data={"csrf_token": self.owner_csrf, "x": "0.25", "y": "0.75"},
        )

        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual(404, hidden.status_code)
        self.assertEqual(202, accepted.status_code)
        self.assertEqual({"accepted": True}, accepted.json())
        with session_scope(self.engine) as db:
            self.assertEqual(
                {"kind": "click", "x": 0.25, "y": 0.75},
                ScanSessionService(db).claim_interaction(scan_id),
            )

    def test_owner_can_send_verification_code_without_exposing_it_in_response(self):
        scan_id = self._awaiting_scan()

        response = self.owner_client.post(
            f"/accounts/scan/{scan_id}/interact",
            data={
                "csrf_token": self.owner_csrf,
                "kind": "text",
                "text": "123456",
            },
        )

        self.assertEqual(202, response.status_code)
        self.assertEqual({"accepted": True}, response.json())
        self.assertNotIn("123456", response.text)
        with session_scope(self.engine) as db:
            self.assertEqual(
                {"kind": "text", "text": "123456"},
                ScanSessionService(db).claim_interaction(
                    scan_id, CookieCipher(b"c" * 32)
                ),
            )

    def test_verification_code_route_rejects_non_numeric_or_wrong_length(self):
        scan_id = self._awaiting_scan()

        for value in ("123", "123456789", "12a456"):
            with self.subTest(value=value):
                response = self.owner_client.post(
                    f"/accounts/scan/{scan_id}/interact",
                    data={
                        "csrf_token": self.owner_csrf,
                        "kind": "text",
                        "text": value,
                    },
                )
                self.assertEqual(400, response.status_code)
                self.assertNotIn(value, response.text)

    def test_qr_before_publication_uses_a_stable_unavailable_error(self):
        response = self.owner_client.post(
            "/accounts/scan", data={"csrf_token": self.owner_csrf}
        )
        self.assertEqual(201, response.status_code)

        qr = self.owner_client.get(f"/accounts/scan/{response.json()['id']}/qr")

        self.assertEqual(404, qr.status_code)
        self.assertEqual("no-store", qr.headers["cache-control"])
        self.assertEqual(
            {"error": "qr_unavailable", "message": "二维码尚未就绪"}, qr.json()
        )

    def test_owner_can_cancel_with_csrf_and_cross_user_is_hidden(self):
        scan_id = self._awaiting_scan()

        no_csrf = self.owner_client.post(f"/accounts/scan/{scan_id}/cancel")
        self.assertEqual(403, no_csrf.status_code)
        hidden = self.other_client.post(
            f"/accounts/scan/{scan_id}/cancel",
            data={"csrf_token": self.other_csrf},
        )
        self.assertEqual(404, hidden.status_code)
        cancelled = self.owner_client.post(
            f"/accounts/scan/{scan_id}/cancel",
            data={"csrf_token": self.owner_csrf},
        )
        self.assertEqual(200, cancelled.status_code)
        self.assertEqual("cancelled", cancelled.json()["status"])
        self.assertEqual("no-store", cancelled.headers["cache-control"])
        with session_scope(self.engine) as db:
            scan = db.get(DouyinLoginSession, scan_id)
            self.assertIsNone(scan.qr_png)
            self.assertIsNone(scan.slot)

    def test_owner_only_rename_accepts_1_to_64_characters(self):
        hidden = self.other_client.post(
            f"/accounts/{self.account_id}/rename",
            data={"csrf_token": self.other_csrf, "display_name": "越权改名"},
            follow_redirects=False,
        )
        self.assertEqual(404, hidden.status_code)

        invalid = self.owner_client.post(
            f"/accounts/{self.account_id}/rename",
            data={"csrf_token": self.owner_csrf, "display_name": " "},
            follow_redirects=False,
        )
        self.assertEqual(400, invalid.status_code)
        renamed = self.owner_client.post(
            f"/accounts/{self.account_id}/rename",
            data={"csrf_token": self.owner_csrf, "display_name": " 我的主账号 "},
            follow_redirects=False,
        )
        self.assertEqual(303, renamed.status_code)
        self.assertEqual("/accounts", renamed.headers["location"])
        with session_scope(self.engine) as db:
            self.assertEqual("我的主账号", db.get(DouyinAccount, self.account_id).display_name)

    def test_account_page_has_qr_dialog_and_rename_but_no_manual_credentials(self):
        response = self.owner_client.get("/accounts")

        self.assertEqual(200, response.status_code)
        self.assertIn("扫码绑定抖音账号", response.text)
        self.assertIn("<dialog", response.text)
        self.assertIn(
            'src="/static/account_scan.js?v=20260826-4"', response.text
        )
        self.assertIn("修改备注", response.text)
        self.assertIn("当前仅支持短信验证码", response.text)
        self.assertIn(f'action="/accounts/{self.account_id}/rename"', response.text)
        self.assertNotIn('textarea name="cookies"', response.text)
        self.assertNotIn('name="cookies"', response.text)
        self.assertNotIn("Cookie JSON", response.text)
        self.assertNotIn("Token", response.text)
        self.assertNotIn("storage_state", response.text)

    def test_account_page_preloads_only_when_user_has_no_bound_account(self):
        owner_page = self.owner_client.get("/accounts")
        other_page = self.other_client.get("/accounts")

        self.assertIn('data-preload="false"', owner_page.text)
        self.assertIn('data-preload="true"', other_page.text)

    def test_old_manual_account_submission_is_rejected(self):
        response = self.owner_client.post(
            "/accounts",
            data={
                "csrf_token": self.owner_csrf,
                "display_name": "手工账号",
                "cookies": '[{"name":"sid","value":"credential-marker"}]',
            },
            follow_redirects=False,
        )

        self.assertEqual(405, response.status_code)
        with session_scope(self.engine) as db:
            self.assertEqual(1, len(db.scalars(select(DouyinAccount)).all()))


if __name__ == "__main__":
    unittest.main()
