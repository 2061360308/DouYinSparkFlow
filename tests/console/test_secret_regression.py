import subprocess
import unittest
import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spark_console.crypto import CookieCipher
from spark_console.db import create_schema
from spark_console.models import AuditEvent, User
from spark_console.services.accounts import AccountService
from spark_console.services.audits import AuditService


class SecretRegressionTests(unittest.TestCase):
    def test_tracked_files_contain_no_runtime_secret_files(self):
        if not Path(".git").exists():
            self.skipTest("production image intentionally excludes Git metadata")
        tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()
        forbidden = {"usersData.json", "spark.db", "cookie.key", "session.key", ".env.console"}
        self.assertTrue(forbidden.isdisjoint(Path(path).name for path in tracked))

    def test_storage_credentials_never_enter_public_or_audit_serialization(self):
        engine = create_engine("sqlite:///:memory:")
        create_schema(engine)
        with Session(engine) as session:
            user = User(username="secret-check", password_hash="hash")
            session.add(user)
            session.flush()
            accounts = AccountService(session, CookieCipher(b"s" * 32), AuditService(session))
            accounts.create_from_storage_state(
                user.id,
                "扫码账号",
                {
                    "cookies": [{"name": "sid", "value": "page-cookie-marker"}],
                    "origins": [
                        {
                            "origin": "https://www.douyin.com",
                            "localStorage": [
                                {"name": "token", "value": "page-storage-marker"}
                            ],
                        }
                    ],
                },
            )
            session.flush()

            serialized_page = json.dumps(accounts.list_owned(user.id), ensure_ascii=False)
            serialized_audit = json.dumps(
                [
                    {
                        "action": event.action,
                        "detail": event.detail,
                    }
                    for event in session.scalars(select(AuditEvent)).all()
                ],
                ensure_ascii=False,
            )
            self.assertFalse("page-cookie-marker" in serialized_page + serialized_audit)
            self.assertFalse("page-storage-marker" in serialized_page + serialized_audit)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
