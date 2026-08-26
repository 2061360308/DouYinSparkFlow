from __future__ import annotations

import json

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from spark_console.credentials import CredentialError, CredentialPayload
from spark_console.crypto import CookieCipher
from spark_console.models import DouyinAccount, DouyinAccountIdentity, SparkTask, utc_now
from spark_console.services import NotFound, ValidationError
from spark_console.services.audits import AuditService


class AccountService:
    def __init__(self, session: Session, cipher: CookieCipher, audit: AuditService):
        self.session = session
        self.cipher = cipher
        self.audit = audit

    def create(self, owner_id: str, display_name: str, cookies: bytes | str) -> DouyinAccount:
        name = self._validated_display_name(display_name)
        raw = cookies.encode("utf-8") if isinstance(cookies, str) else cookies
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValidationError("Cookie 必须是有效的 JSON") from error
        if not isinstance(parsed, list) or not parsed:
            raise ValidationError("Cookie JSON 必须是非空数组")
        sealed = self.cipher.encrypt(raw)
        account = DouyinAccount(
            owner_user_id=owner_id,
            display_name=name,
            encrypted_cookies=sealed.ciphertext,
            cookie_nonce=sealed.nonce,
        )
        self.session.add(account)
        self.session.flush()
        self.audit.write(owner_id, "account.created", "douyin_account", account.id)
        return account

    def create_from_storage_state(
        self,
        owner_id: str,
        display_name: str,
        storage_state: dict,
        douyin_unique_id: str | None = None,
    ) -> DouyinAccount:
        name = self._validated_display_name(display_name)
        normalized_unique_id = (
            douyin_unique_id.strip() if douyin_unique_id is not None else None
        )
        if normalized_unique_id == "":
            normalized_unique_id = None
        if normalized_unique_id is not None and len(normalized_unique_id) > 64:
            raise ValidationError("抖音号不能超过 64 个字符")

        envelope = {"version": 2, "storage_state": storage_state}
        try:
            raw = json.dumps(
                envelope, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            CredentialPayload.parse(raw, 2)
        except (CredentialError, TypeError, ValueError, UnicodeEncodeError):
            raise ValidationError("浏览器凭据格式无效") from None
        sealed = self.cipher.encrypt(raw)
        account = DouyinAccount(
            owner_user_id=owner_id,
            display_name=name,
            encrypted_cookies=sealed.ciphertext,
            cookie_nonce=sealed.nonce,
            cookie_version=2,
            validation_state="valid",
            last_verified_at=utc_now(),
        )
        self.session.add(account)
        self.session.flush()
        self.session.add(
            DouyinAccountIdentity(
                account_id=account.id, douyin_unique_id=normalized_unique_id
            )
        )
        self.audit.write(owner_id, "account.created", "douyin_account", account.id)
        return account

    def rename_owned(
        self, owner_id: str, account_id: str, display_name: str
    ) -> DouyinAccount:
        account = self.get_owned(owner_id, account_id)
        account.display_name = self._validated_display_name(display_name)
        self.session.flush()
        self.audit.write(owner_id, "account.renamed", "douyin_account", account.id)
        return account

    def get_owned(self, owner_id: str, account_id: str) -> DouyinAccount:
        account = self.session.scalar(
            select(DouyinAccount).where(
                DouyinAccount.id == account_id,
                DouyinAccount.owner_user_id == owner_id,
            )
        )
        if account is None:
            raise NotFound("account not found")
        return account

    def list_owned(self, owner_id: str) -> list[dict[str, str]]:
        accounts = self.session.scalars(
            select(DouyinAccount)
            .where(DouyinAccount.owner_user_id == owner_id)
            .order_by(DouyinAccount.created_at)
        ).all()
        return [
            {"id": item.id, "display_name": item.display_name, "validation_state": item.validation_state}
            for item in accounts
        ]

    def decrypt_for_worker(self, account_id: str) -> bytearray:
        account = self.session.get(DouyinAccount, account_id)
        if account is None:
            raise NotFound("account not found")
        return bytearray(self.cipher.decrypt(account.encrypted_cookies, account.cookie_nonce))

    def delete_owned(self, owner_id: str, account_id: str) -> None:
        account = self.get_owned(owner_id, account_id)
        self.session.execute(
            update(SparkTask)
            .where(SparkTask.douyin_account_id == account.id)
            .values(enabled=False, douyin_account_id=None)
        )
        account.encrypted_cookies = b""
        account.cookie_nonce = b""
        self.session.flush()
        self.session.delete(account)
        self.audit.write(owner_id, "account.deleted", "douyin_account", account_id)

    @staticmethod
    def _validated_display_name(display_name: str) -> str:
        name = display_name.strip()
        if not name or len(name) > 64:
            raise ValidationError("账号名称须为 1–64 个字符")
        return name
