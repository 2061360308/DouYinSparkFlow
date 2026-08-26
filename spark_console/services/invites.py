from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from spark_console.crypto import CookieCipher
from spark_console.models import InviteCode, InviteCodeSecret, utc_now
from spark_console.services import ValidationError
from spark_console.services.audits import AuditService


def _digest(code: str) -> str:
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


class InviteService:
    def __init__(
        self,
        session: Session,
        audit: AuditService,
        cipher: CookieCipher | None = None,
        now=utc_now,
    ):
        self.session = session
        self.audit = audit
        self.cipher = cipher
        self.now = now

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def create(
        self, actor_id: str, lifetime: timedelta = timedelta(days=7)
    ) -> tuple[InviteCode, str]:
        now = self._now()
        plaintext = secrets.token_urlsafe(24)
        invite = InviteCode(
            code_hash=_digest(plaintext),
            created_by_user_id=actor_id,
            expires_at=now + lifetime,
            created_at=now,
        )
        self.session.add(invite)
        self.session.flush()
        if self.cipher is not None:
            encrypted = self.cipher.encrypt(plaintext.encode("utf-8"))
            self.session.add(
                InviteCodeSecret(
                    invite_id=invite.id,
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                )
            )
        self.audit.write(actor_id, "invite.created", "invite_code", invite.id)
        return invite, plaintext

    def reveal(self, invite_id: str) -> str | None:
        if self.cipher is None:
            return None
        secret = self.session.get(InviteCodeSecret, invite_id)
        invite = self.session.get(InviteCode, invite_id)
        if secret is None or invite is None:
            return None
        try:
            plaintext = self.cipher.decrypt(secret.ciphertext, secret.nonce).decode(
                "utf-8"
            )
        except (ValueError, UnicodeDecodeError):
            return None
        return plaintext if _digest(plaintext) == invite.code_hash else None

    def consume(self, code: str, user_id: str) -> None:
        invite = self.session.scalar(
            select(InviteCode).where(InviteCode.code_hash == _digest(code))
        )
        if invite is None:
            raise ValidationError("注册信息或邀请码无效")

        now = self._now()
        result = self.session.execute(
            update(InviteCode)
            .execution_options(synchronize_session="fetch")
            .where(
                InviteCode.id == invite.id,
                InviteCode.used_at.is_(None),
                InviteCode.revoked_at.is_(None),
                InviteCode.expires_at > now,
            )
            .values(used_by_user_id=user_id, used_at=now)
        )
        if result.rowcount != 1:
            raise ValidationError("注册信息或邀请码无效")
        self.audit.write(user_id, "invite.consumed", "invite_code", invite.id)

    def list_all(self) -> list[InviteCode]:
        return list(
            self.session.scalars(
                select(InviteCode).order_by(InviteCode.created_at.desc())
            )
        )

    def revoke(self, actor_id: str, invite_id: str) -> None:
        now = self._now()
        result = self.session.execute(
            update(InviteCode)
            .execution_options(synchronize_session="fetch")
            .where(
                InviteCode.id == invite_id,
                InviteCode.used_at.is_(None),
                InviteCode.revoked_at.is_(None),
                InviteCode.expires_at > now,
            )
            .values(revoked_at=now)
        )
        if result.rowcount != 1:
            raise ValidationError("注册信息或邀请码无效")
        invite = self.session.get(InviteCode, invite_id)
        if invite is not None:
            self.session.expire(invite)
        self.audit.write(actor_id, "invite.revoked", "invite_code", invite_id)

    def delete(self, actor_id: str, invite_id: str) -> None:
        invite = self.session.get(InviteCode, invite_id)
        if invite is None:
            raise ValidationError("注册信息或邀请码无效")
        self.session.delete(invite)
        self.session.flush()
        self.audit.write(actor_id, "invite.deleted", "invite_code", invite_id)
