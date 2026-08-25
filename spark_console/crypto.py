from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: bytes
    nonce: bytes


class CookieCipher:
    _AAD = b"douyin-cookie-v1"

    def __init__(self, key: bytes):
        if len(key) != 32:
            raise ValueError("AES-256-GCM requires a 32-byte key")
        self._aes = AESGCM(key)

    def encrypt(self, payload: bytes) -> EncryptedPayload:
        nonce = os.urandom(12)
        return EncryptedPayload(
            ciphertext=self._aes.encrypt(nonce, payload, self._AAD), nonce=nonce
        )

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> bytes:
        return self._aes.decrypt(nonce, ciphertext, self._AAD)
