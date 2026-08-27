import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select

from spark_console.config import Settings
from spark_console.crypto import CookieCipher
from spark_console.db import create_schema, session_scope
from spark_console.models import (
    DouyinAccount,
    DouyinContactIdentity,
    DouyinConversation,
    SparkTask,
    SparkTaskTargetIdentity,
    TaskRun,
    User,
)
from spark_console.security import PasswordService
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService
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

    def test_logged_in_user_has_change_password_navigation(self):
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
                "new_password_confirmation": "Permanent-123!",
            },
        )

        dashboard = self.client.get("/dashboard")
        change_page = self.client.get("/change-password")

        self.assertIn('href="/change-password"', dashboard.text)
        self.assertIn("修改密码", change_page.text)
        self.assertIn('name="new_password_confirmation"', change_page.text)

    def test_change_password_page_uses_centered_security_layout(self):
        self.login()

        page = self.client.get("/change-password")

        self.assertEqual(200, page.status_code)
        self.assertIn('class="security-page"', page.text)
        self.assertIn('class="auth-card security-card"', page.text)

    def test_change_password_rejects_mismatched_confirmation(self):
        self.login()
        page = self.client.get("/change-password")
        marker = 'name="csrf_token" value="'
        csrf = page.text.split(marker, 1)[1].split('"', 1)[0]

        response = self.client.post(
            "/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "Temporary-123!",
                "new_password": "Permanent-123!",
                "new_password_confirmation": "Different-123!",
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertIn("两次输入的新密码不一致", response.text)

    def test_wrong_password_renders_visible_login_error(self):
        response = self.client.post(
            "/login",
            data={"username": "friend", "password": "definitely-wrong"},
            follow_redirects=False,
        )
        self.assertEqual(400, response.status_code)
        self.assertIn("用户名或密码错误", response.text)
        self.assertIn('name="password"', response.text)

    def test_login_page_offers_invite_registration(self):
        response = self.client.get("/login")

        self.assertEqual(200, response.status_code)
        self.assertIn('href="/register"', response.text)

    def test_login_stylesheet_uses_same_origin_path_behind_https_proxy(self):
        with TestClient(
            create_app(self.settings, self.engine), base_url="http://internal"
        ) as client:
            response = client.get(
                "/login",
                headers={
                    "host": "wangze.oilu.cn",
                    "x-forwarded-proto": "https",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertIn('href="/static/app.css?', response.text)
        self.assertNotIn('href="http://wangze.oilu.cn/static/app.css"', response.text)

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

    def test_account_page_never_accepts_or_renders_manual_credentials(self):
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
                "new_password_confirmation": "Permanent-123!",
            },
        )
        page = self.client.get("/accounts")
        self.assertNotIn('name="cookies"', page.text)
        self.assertNotIn("Cookie JSON", page.text)
        response = self.client.post(
            "/accounts",
            data={
                "csrf_token": page.text.split(marker, 1)[1].split('"', 1)[0],
                "display_name": "主账号",
                "cookies": '[{"name":"sessionid","value":"sessionid-secret"}]',
            },
            follow_redirects=False,
        )
        self.assertEqual(405, response.status_code)

    def test_task_page_loads_saved_account_conversations_without_browser_access(self):
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
                "new_password_confirmation": "Permanent-123!",
            },
        )
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "我的抖音",
                b'[{"name":"sessionid","value":"secret-marker","url":"https://www.douyin.com"}]',
            )
            account_id = account.id
            session.add_all(
                [
                    DouyinConversation(account_id=account_id, display_name="wzlovegsy"),
                    DouyinConversation(account_id=account_id, display_name="gsy"),
                    DouyinContactIdentity(
                        account_id=account_id,
                        sec_uid="stable-user-id",
                        unique_id="wzlovegsy",
                        nickname="新的昵称",
                        remark_name="我的备注",
                    ),
                ]
            )
        tasks_page = self.client.get("/tasks")
        response = self.client.get(f"/accounts/{account_id}/conversations")

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "items": [
                    {"name": "gsy", "sec_uid": None},
                    {"name": "我的备注", "sec_uid": "stable-user-id"},
                ]
            },
            response.json(),
        )
        self.assertIn('list="task-target-options"', tasks_page.text)
        self.assertIn('name="target_sec_uid"', tasks_page.text)
        self.assertIn("读取好友列表", tasks_page.text)
        self.assertNotIn("secret-marker", response.text)

    def test_new_task_binds_selected_stable_contact_identity(self):
        self.login()
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "我的抖音",
                b'[{"name":"sessionid","value":"secret","url":"https://www.douyin.com"}]',
            )
            session.add(
                DouyinContactIdentity(
                    account_id=account.id,
                    sec_uid="stable-user-id",
                    nickname="新的昵称",
                )
            )
            account_id = account.id
        page = self.client.get("/tasks")
        csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        response = self.client.post(
            "/tasks",
            data={
                "csrf_token": csrf,
                "account_id": account_id,
                "target_name": "新的昵称",
                "target_sec_uid": "stable-user-id",
                "send_time": "09:00",
                "message_template": "今日火花",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        with session_scope(self.engine) as session:
            task = session.scalar(select(SparkTask).where(SparkTask.owner_user_id == user.id))
            binding = session.get(SparkTaskTargetIdentity, task.id)
            self.assertEqual("stable-user-id", binding.sec_uid)

    def test_user_can_open_and_submit_task_editor(self):
        self.login()
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = AccountService(
                session,
                CookieCipher(self.settings.cookie_key_file.read_bytes()),
                AuditService(session),
            ).create(
                user.id,
                "我的抖音",
                b'[{"name":"sessionid","value":"secret","url":"https://www.douyin.com"}]',
            )
            session.add(
                DouyinContactIdentity(
                    account_id=account.id,
                    sec_uid="edited-stable-id",
                    nickname="编辑后的好友",
                )
            )
            task = SparkTask(
                owner_user_id=user.id,
                douyin_account_id=account.id,
                target_name="旧好友",
                send_time="09:00",
                message_template="旧消息",
                enabled=True,
            )
            session.add(task)
            session.flush()
            task_id = task.id
            account_id = account.id

        tasks_page = self.client.get("/tasks")
        self.assertIn(f'href="/tasks/{task_id}/edit"', tasks_page.text)
        edit_page = self.client.get(f"/tasks/{task_id}/edit")
        self.assertEqual(200, edit_page.status_code)
        self.assertIn("编辑续火任务", edit_page.text)
        self.assertIn('value="09:00"', edit_page.text)
        self.assertIn("旧消息", edit_page.text)
        csrf = edit_page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]

        response = self.client.post(
            f"/tasks/{task_id}/edit",
            data={
                "csrf_token": csrf,
                "account_id": account_id,
                "target_name": "编辑后的好友",
                "target_sec_uid": "edited-stable-id",
                "send_time": "21:30",
                "message_template": "编辑后的消息",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)
        self.assertEqual("/tasks", response.headers["location"])
        with session_scope(self.engine) as session:
            task = session.get(SparkTask, task_id)
            binding = session.get(SparkTaskTargetIdentity, task_id)
            self.assertEqual("编辑后的好友", task.target_name)
            self.assertEqual("21:30", task.send_time)
            self.assertEqual("编辑后的消息", task.message_template)
            self.assertEqual("edited-stable-id", binding.sec_uid)

    def test_run_history_uses_chinese_status_and_shanghai_time(self):
        with session_scope(self.engine) as session:
            user = session.scalar(select(User).where(User.username == "friend"))
            user.must_change_password = False
            account = DouyinAccount(
                owner_user_id=user.id,
                display_name="我的抖音",
                encrypted_cookies=b"encrypted",
                cookie_nonce=b"nonce",
            )
            session.add(account)
            session.flush()
            task = SparkTask(
                owner_user_id=user.id,
                douyin_account_id=account.id,
                target_name="繁花",
                send_time="16:36",
                message_template="今日火花",
                enabled=True,
            )
            session.add(task)
            session.flush()
            session.add(
                TaskRun(
                    task_id=task.id,
                    scheduled_for=datetime(2026, 8, 26, 8, 36, tzinfo=timezone.utc),
                    status="success",
                    stage="submitted",
                    error_code="delivery_confirmation_unavailable",
                    error_summary="消息已提交，页面未能二次确认",
                    started_at=datetime(2026, 8, 26, 8, 36, 2, tzinfo=timezone.utc),
                    finished_at=datetime(2026, 8, 26, 8, 36, 8, tzinfo=timezone.utc),
                )
            )
            session.add(
                TaskRun(
                    task_id=task.id,
                    scheduled_for=datetime(2026, 8, 25, 8, 36, tzinfo=timezone.utc),
                    status="failed",
                    stage="worker_error",
                    error_code="unexpected_error",
                    error_summary="任务执行发生意外异常，Worker 已继续运行",
                )
            )
        self.login()

        response = self.client.get("/runs")

        self.assertEqual(200, response.status_code)
        self.assertIn("16:36", response.text)
        self.assertIn("成功", response.text)
        self.assertIn("已提交发送", response.text)
        self.assertIn("消息已提交，页面未能二次确认", response.text)
        self.assertIn('class="run-note info"', response.text)
        self.assertIn("失败", response.text)
        self.assertIn("执行异常", response.text)
        self.assertIn("繁花", response.text)
        self.assertIn('class="run-timeline"', response.text)


if __name__ == "__main__":
    unittest.main()
