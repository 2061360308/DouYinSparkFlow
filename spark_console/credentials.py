from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class CredentialError(ValueError):
    """A credential payload does not match its declared format version."""


def _invalid() -> CredentialError:
    return CredentialError("credential payload is invalid")


def _load_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise _invalid() from None


def _valid_cookie(cookie: object) -> bool:
    return (
        isinstance(cookie, dict)
        and isinstance(cookie.get("name"), str)
        and isinstance(cookie.get("value"), str)
    )


def _valid_origin(origin: object) -> bool:
    if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
        return False
    local_storage = origin.get("localStorage")
    return isinstance(local_storage, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("value"), str)
        for item in local_storage
    )


@dataclass(frozen=True)
class CredentialPayload:
    _context: dict[str, object]
    _cookies: list[dict[str, object]]

    @classmethod
    def parse(cls, raw: bytes, version: int) -> CredentialPayload:
        if type(version) is not int:
            raise CredentialError("credential version is unsupported")
        if version == 1:
            cookies = _load_json(raw)
            if (
                not isinstance(cookies, list)
                or not cookies
                or not all(_valid_cookie(cookie) for cookie in cookies)
            ):
                raise _invalid()
            return cls({}, cookies)

        if version == 2:
            envelope = _load_json(raw)
            if (
                not isinstance(envelope, dict)
                or type(envelope.get("version")) is not int
                or envelope.get("version") != 2
                or not isinstance(envelope.get("storage_state"), dict)
            ):
                raise _invalid()
            storage_state = envelope["storage_state"]
            cookies = storage_state.get("cookies")
            origins = storage_state.get("origins")
            if (
                not isinstance(cookies, list)
                or not cookies
                or not all(_valid_cookie(cookie) for cookie in cookies)
                or not isinstance(origins, list)
                or not all(_valid_origin(origin) for origin in origins)
            ):
                raise _invalid()
            return cls({"storage_state": storage_state}, [])

        raise CredentialError("credential version is unsupported")

    def context_options(self) -> dict[str, object]:
        return self._context

    def cookies_to_add(self) -> list[dict[str, object]]:
        return self._cookies
