from __future__ import annotations

import secrets
import string

from sqlalchemy import select
from sqlalchemy.orm import Session

from spark_console.models import User, WebSession
from spark_console.security import PasswordService
from spark_console.services import Conflict, NotFound, ValidationError
from spark_console.services.audits import AuditService


class UserService:
    def __init__(self, session: Session, passwords: PasswordService, audit: AuditService):
        self.session = session
        self.passwords = passwords
        self.audit = audit

    @staticmethod
    def temporary_password() -> str:
        alphabet = string.ascii_letters + string.digits + "!@#$%"
        return "".join(secrets.choice(alphabet) for _ in range(18))

    def create(self, username: str, password: str | None = None, role: str = "user") -> tuple[User, str]:
        name = username.strip().lower()
        if not (3 <= len(name) <= 32) or not all(c.isalnum() or c in "_-" for c in name):
            raise ValidationError("用户名须为 3–32 位字母、数字、下划线或短横线")
        if role not in {"user", "admin"}:
            raise ValidationError("invalid role")
        if self.session.scalar(select(User).where(User.username == name)):
            raise Conflict("用户名已存在")
        temporary = password or self.temporary_password()
        user = User(
            username=name,
            password_hash=self.passwords.hash(temporary),
            role=role,
            must_change_password=True,
        )
        self.session.add(user)
        self.session.flush()
        self.audit.write(None, "user.created", "user", user.id)
        return user, temporary

    def authenticate(self, username: str, password: str) -> User | None:
        user = self.session.scalar(select(User).where(User.username == username.strip().lower()))
        if user is None or user.status != "active":
            return None
        if not self.passwords.verify(user.password_hash, password):
            user.failed_login_count += 1
            return None
        user.failed_login_count = 0
        user.locked_until = None
        return user

    def reset_password(self, actor_id: str, user_id: str) -> str:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFound("user not found")
        temporary = self.temporary_password()
        user.password_hash = self.passwords.hash(temporary)
        user.must_change_password = True
        self.session.query(WebSession).filter(WebSession.user_id == user.id).delete()
        self.audit.write(actor_id, "user.password_reset", "user", user.id)
        return temporary

    def set_disabled(self, actor_id: str, user_id: str, disabled: bool) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise NotFound("user not found")
        user.status = "disabled" if disabled else "active"
        self.audit.write(actor_id, "user.disabled" if disabled else "user.enabled", "user", user.id)
        return user
