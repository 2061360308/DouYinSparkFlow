from __future__ import annotations

import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit


_LEGACY_COOKIE_KEYS = {
    "name",
    "value",
    "url",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
    "partitionKey",
    "_crHasCrossSiteAncestor",
}
_STORAGE_COOKIE_REQUIRED_KEYS = {
    "name",
    "value",
    "domain",
    "path",
    "expires",
    "httpOnly",
    "secure",
    "sameSite",
}
_STORAGE_COOKIE_OPTIONAL_KEYS = {"partitionKey", "_crHasCrossSiteAncestor"}
_MAX_COOKIE_EXPIRES = 253_402_300_799
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_HOST_DELIMITERS = frozenset("%/\\[]^|@#?")


class CredentialError(ValueError):
    """A credential payload does not match its declared format version."""


def _invalid() -> CredentialError:
    return CredentialError("credential payload is invalid")


def _load_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise _invalid() from None


def _valid_url(value: object, *, origin_only: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port
        hostname = parsed.hostname
    except (UnicodeError, ValueError):
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or _canonical_host(hostname, allow_ipv6=True) is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    return not origin_only or (
        parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
    )


def _canonical_host(value: str, *, allow_ipv6: bool) -> str | None:
    try:
        decoded = unquote_to_bytes(value).decode("utf-8")
    except UnicodeError:
        return None
    if (
        not decoded
        or any(character in _HOST_DELIMITERS for character in decoded)
        or any(
            ord(character) <= 0x20
            or ord(character) == 0x7F
            or character.isspace()
            or unicodedata.category(character).startswith("C")
            for character in decoded
        )
    ):
        return None

    if ":" in decoded:
        if not allow_ipv6 or ":" not in value:
            return None
        try:
            return ipaddress.IPv6Address(decoded).compressed
        except ipaddress.AddressValueError:
            return None

    host = decoded[:-1] if decoded.endswith(".") else decoded
    if not host:
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
        unicode_host = ascii_host.encode("ascii").decode("idna")
        if any(
            label and unicodedata.category(label[0]).startswith("M")
            for label in unicode_host.split(".")
        ) or unicode_host.encode("idna").decode("ascii").lower() != ascii_host:
            return None
    except UnicodeError:
        return None

    labels = ascii_host.split(".")
    if not labels or len(ascii_host) > 253:
        return None
    if labels[-1].isdigit():
        if len(labels) != 4 or not all(label.isdigit() for label in labels):
            return None
        try:
            return str(ipaddress.IPv4Address(ascii_host))
        except ipaddress.AddressValueError:
            return None
    if not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return None
    return ascii_host


def _valid_domain(value: object) -> bool:
    if not isinstance(value, str):
        return False
    host = value[1:] if value.startswith(".") else value
    return _canonical_host(host, allow_ipv6=False) is not None


def _valid_expires(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value == -1 or 0 <= value <= _MAX_COOKIE_EXPIRES)
    )


def _valid_cookie_options(cookie: dict[str, object]) -> bool:
    if "expires" in cookie and not _valid_expires(cookie["expires"]):
        return False
    for key in ("httpOnly", "secure"):
        if key in cookie and type(cookie[key]) is not bool:
            return False
    if "sameSite" in cookie:
        same_site = cookie["sameSite"]
        if not isinstance(same_site, str) or same_site not in {
            "Strict",
            "Lax",
            "None",
        }:
            return False
    if "partitionKey" in cookie and not _valid_url(
        cookie["partitionKey"], origin_only=True
    ):
        return False
    if "_crHasCrossSiteAncestor" in cookie and (
        type(cookie["_crHasCrossSiteAncestor"]) is not bool
        or "partitionKey" not in cookie
    ):
        return False
    return True


def _valid_legacy_cookie(cookie: object) -> bool:
    if (
        not isinstance(cookie, dict)
        or not set(cookie).issubset(_LEGACY_COOKIE_KEYS)
        or not isinstance(cookie.get("name"), str)
        or not isinstance(cookie.get("value"), str)
    ):
        return False
    has_url = "url" in cookie
    has_domain_path = "domain" in cookie or "path" in cookie
    if has_url == has_domain_path:
        return False
    if has_url:
        if not _valid_url(cookie["url"]):
            return False
    elif not (
        _valid_domain(cookie.get("domain"))
        and isinstance(cookie.get("path"), str)
        and cookie["path"].startswith("/")
    ):
        return False
    return _valid_cookie_options(cookie)


def _valid_storage_cookie(cookie: object) -> bool:
    if not isinstance(cookie, dict):
        return False
    keys = set(cookie)
    if (
        not _STORAGE_COOKIE_REQUIRED_KEYS.issubset(keys)
        or not keys.issubset(
            _STORAGE_COOKIE_REQUIRED_KEYS | _STORAGE_COOKIE_OPTIONAL_KEYS
        )
        or not isinstance(cookie["name"], str)
        or not isinstance(cookie["value"], str)
        or not _valid_domain(cookie["domain"])
        or not isinstance(cookie["path"], str)
        or not cookie["path"].startswith("/")
    ):
        return False
    return _valid_cookie_options(cookie)


def _valid_origin(origin: object) -> bool:
    if (
        not isinstance(origin, dict)
        or set(origin) != {"origin", "localStorage"}
        or not _valid_url(origin.get("origin"), origin_only=True)
    ):
        return False
    local_storage = origin.get("localStorage")
    return isinstance(local_storage, list) and all(
        isinstance(item, dict)
        and set(item) == {"name", "value"}
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
                or not all(_valid_legacy_cookie(cookie) for cookie in cookies)
            ):
                raise _invalid()
            return cls({}, cookies)

        if version == 2:
            envelope = _load_json(raw)
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"version", "storage_state"}
                or type(envelope.get("version")) is not int
                or envelope.get("version") != 2
                or not isinstance(envelope.get("storage_state"), dict)
            ):
                raise _invalid()
            storage_state = envelope["storage_state"]
            if set(storage_state) != {"cookies", "origins"}:
                raise _invalid()
            cookies = storage_state.get("cookies")
            origins = storage_state.get("origins")
            if (
                not isinstance(cookies, list)
                or not cookies
                or not all(_valid_storage_cookie(cookie) for cookie in cookies)
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
