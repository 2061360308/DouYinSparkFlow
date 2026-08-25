from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from spark_console.models import WebSession


class PasswordService:
    def __init__(self) -> None:
        self._hasher = PasswordHasher()

    def hash(self, password: str) -> str:
        if len(password) < 12:
            raise ValueError("password must contain at least 12 characters")
        return self._hasher.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded, password)
        except (VerifyMismatchError, InvalidHashError):
            return False


class SessionService:
    def __init__(self, session_key: bytes, lifetime: timedelta = timedelta(hours=8)):
        if len(session_key) < 32:
            raise ValueError("session key must contain at least 32 bytes")
        self._key = session_key
        self._lifetime = lifetime

    @staticmethod
    def token_hash(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("ascii")).hexdigest()

    def create_record(
        self, user_id: str, now: datetime | None = None
    ) -> tuple[str, WebSession]:
        issued_at = now or datetime.now(timezone.utc)
        raw = secrets.token_urlsafe(32)
        record = WebSession(
            user_id=user_id,
            token_hash=self.token_hash(raw),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=issued_at + self._lifetime,
        )
        return raw, record

    def matches(self, raw_token: str, record: WebSession) -> bool:
        return secrets.compare_digest(self.token_hash(raw_token), record.token_hash)


class CsrfService:
    @staticmethod
    def issue() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def verify(expected: str, supplied: str) -> bool:
        return secrets.compare_digest(expected, supplied)
