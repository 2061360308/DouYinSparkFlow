import asyncio
import hashlib
import json
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from spark_console.credentials import CredentialError, CredentialPayload
from spark_console.executor import DouyinExecutor


class CredentialPayloadTests(unittest.TestCase):
    def test_version_one_cookie_array_is_added_after_context_creation(self):
        raw = b'[{"name":"sid","value":"legacy-secret","domain":".douyin.com","path":"/"}]'

        payload = CredentialPayload.parse(raw, 1)

        self.assertEqual(0, len(payload.context_options()))
        serialized = json.dumps(
            payload.cookies_to_add(), ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(
            "ae0bc243fe74434d60a4232494f4b386939fb856e26dddb60938fb3b6475fc5c",
            hashlib.sha256(serialized).hexdigest(),
        )

    def test_version_two_storage_state_becomes_context_option(self):
        raw = b'{"version":2,"storage_state":{"cookies":[{"name":"sid","value":"secret","domain":".douyin.com","path":"/","expires":-1,"httpOnly":true,"secure":true,"sameSite":"Lax"}],"origins":[]}}'

        payload = CredentialPayload.parse(raw, 2)

        serialized = json.dumps(
            payload.context_options()["storage_state"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        self.assertEqual(
            "c1ebe021888e7fac25261d8b5a6932d2889d41251145a2ebbdec75b37f953de9",
            hashlib.sha256(serialized).hexdigest(),
        )
        self.assertEqual(0, len(payload.cookies_to_add()))

    def test_empty_cookie_arrays_are_rejected_for_both_versions(self):
        fixtures = (
            (b"[]", 1),
            (b'{"version":2,"storage_state":{"cookies":[],"origins":[]}}', 2),
        )
        for raw, version in fixtures:
            with self.subTest(version=version):
                with self.assertRaises(CredentialError):
                    CredentialPayload.parse(raw, version)

    def test_unknown_versions_and_malformed_shapes_are_rejected_without_secret_leak(self):
        fixtures = (
            (b'[{"name":"sid","value":"marker-secret"}]', 3),
            (b'{"version":2,"storage_state":[]}', 2),
            (b'{"version":1,"storage_state":{"cookies":[{"name":"sid","value":"marker-secret"}],"origins":[]}}', 2),
            (b'{"version":2,"storage_state":{"cookies":["marker-secret"],"origins":[]}}', 2),
        )
        secret_marker = "marker-secret"

        for raw, version in fixtures:
            with self.subTest(version=version):
                with self.assertRaises(CredentialError) as caught:
                    CredentialPayload.parse(raw, version)
                self.assertFalse(secret_marker in str(caught.exception))

    def test_version_one_rejects_cookie_shapes_playwright_rejects(self):
        valid = {
            "name": "sid",
            "value": "credential-marker",
            "domain": ".douyin.com",
            "path": "/",
        }
        invalid_cookies = {
            "missing_location": {"name": "sid", "value": "credential-marker"},
            "invalid_domain_percent": {
                "name": "sid",
                "value": "credential-marker",
                "domain": "%",
                "path": "/",
            },
            "invalid_domain_ipv4": {
                "name": "sid",
                "value": "credential-marker",
                "domain": "999.999.999.999",
                "path": "/",
            },
            "invalid_url_percent": {
                "name": "sid",
                "value": "credential-marker",
                "url": "http://%/",
            },
            "invalid_url_ipv4": {
                "name": "sid",
                "value": "credential-marker",
                "url": "http://999.999.999.999/",
            },
            "malformed_url": {
                "name": "sid",
                "value": "credential-marker",
                "url": "http://[",
            },
            "malformed_port": {
                "name": "sid",
                "value": "credential-marker",
                "url": "https://www.douyin.com:not-a-port/",
            },
            "mixed_location": {**valid, "url": "https://www.douyin.com/"},
            "bad_expires_type": {**valid, "expires": "never"},
            "bad_expires_value": {**valid, "expires": -2},
            "bad_http_only": {**valid, "httpOnly": 1},
            "bad_secure": {**valid, "secure": "yes"},
            "bad_same_site": {**valid, "sameSite": "Invalid"},
            "bad_same_site_type": {**valid, "sameSite": ["Lax"]},
            "unknown_field": {**valid, "credential": "unexpected"},
        }

        for case, cookie in invalid_cookies.items():
            with self.subTest(case=case):
                raw = json.dumps([cookie], separators=(",", ":")).encode()
                with self.assertRaises(CredentialError):
                    CredentialPayload.parse(raw, 1)

    def test_version_two_requires_exact_storage_state_shape(self):
        cookie = {
            "name": "sid",
            "value": "credential-marker",
            "domain": ".douyin.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
        valid_state = {"cookies": [cookie], "origins": []}
        invalid_envelopes = {
            "extra_envelope_key": {
                "version": 2,
                "storage_state": valid_state,
                "credential": "unexpected",
            },
            "extra_state_key": {
                "version": 2,
                "storage_state": {**valid_state, "credential": "unexpected"},
            },
            "indexed_db_is_out_of_scope": {
                "version": 2,
                "storage_state": {**valid_state, "indexedDB": []},
            },
            "invalid_storage_cookie_domain": {
                "version": 2,
                "storage_state": {
                    "cookies": [{**cookie, "domain": "%"}],
                    "origins": [],
                },
            },
            "incomplete_cookie": {
                "version": 2,
                "storage_state": {
                    "cookies": [
                        {key: value for key, value in cookie.items() if key != "expires"}
                    ],
                    "origins": [],
                },
            },
            "extra_cookie_key": {
                "version": 2,
                "storage_state": {
                    "cookies": [{**cookie, "credential": "unexpected"}],
                    "origins": [],
                },
            },
            "extra_origin_key": {
                "version": 2,
                "storage_state": {
                    "cookies": [cookie],
                    "origins": [
                        {
                            "origin": "https://www.douyin.com",
                            "localStorage": [],
                            "credential": "unexpected",
                        }
                    ],
                },
            },
            "extra_local_storage_key": {
                "version": 2,
                "storage_state": {
                    "cookies": [cookie],
                    "origins": [
                        {
                            "origin": "https://www.douyin.com",
                            "localStorage": [
                                {
                                    "name": "token",
                                    "value": "credential-marker",
                                    "credential": "unexpected",
                                }
                            ],
                        }
                    ],
                },
            },
        }

        for case, envelope in invalid_envelopes.items():
            with self.subTest(case=case):
                raw = json.dumps(envelope, separators=(",", ":")).encode()
                with self.assertRaises(CredentialError):
                    CredentialPayload.parse(raw, 2)


class _FakePage:
    async def goto(self, *_args, **_kwargs):
        return None


class _FakeContext:
    def __init__(self):
        self.cookies_added = []

    async def add_cookies(self, cookies):
        self.cookies_added.extend(cookies)

    async def new_page(self):
        return _FakePage()

    async def close(self):
        return None


class _FakeBrowser:
    def __init__(self, fail_new_context=False):
        self.context_options = None
        self.context = _FakeContext()
        self.fail_new_context = fail_new_context
        self.close_count = 0

    async def new_context(self, **options):
        self.context_options = options
        if self.fail_new_context:
            raise RuntimeError("context construction failed")
        return self.context

    async def close(self):
        self.close_count += 1


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **_options):
        return self.browser


class _FakePlaywrightManager:
    def __init__(self, browser):
        self.playwright = type(
            "FakePlaywright", (), {"chromium": _FakeChromium(browser)}
        )()

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *_args):
        return None


class ExecutorCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def _execute_until_target_selection(self, raw, version, browser=None):
        from core.web_chat import TargetNotFoundError

        browser = browser or _FakeBrowser()
        async_api = ModuleType("playwright.async_api")
        async_api.async_playwright = lambda: _FakePlaywrightManager(browser)
        playwright = ModuleType("playwright")
        playwright.async_api = async_api
        core_tasks = ModuleType("core.tasks")

        async def confirm_message_sent(*_args, **_kwargs):
            return None

        core_tasks.confirm_message_sent = confirm_message_sent

        async def target_not_found(*_args, **_kwargs):
            raise TargetNotFoundError("fixed public mapping")

        with patch.dict(
            "sys.modules",
            {
                "playwright": playwright,
                "playwright.async_api": async_api,
                "core.tasks": core_tasks,
            },
        ), patch("spark_console.executor.select_web_chat_target", target_not_found):
            result = await DouyinExecutor().execute(
                raw, "目标", "消息", credential_version=version
            )
        return browser, result

    async def test_executor_adds_version_one_cookies_after_empty_context(self):
        raw = b'[{"name":"sid","value":"legacy-executor-marker","domain":".douyin.com","path":"/"}]'

        browser, result = await self._execute_until_target_selection(raw, 1)

        self.assertEqual(0, len(browser.context_options))
        self.assertEqual(1, len(browser.context.cookies_added))
        self.assertEqual("target_not_found", result.error_code)

    async def test_executor_creates_version_two_context_without_adding_cookies(self):
        raw = b'{"version":2,"storage_state":{"cookies":[{"name":"sid","value":"storage-executor-marker","domain":".douyin.com","path":"/","expires":-1,"httpOnly":true,"secure":true,"sameSite":"Lax"}],"origins":[]}}'

        browser, result = await self._execute_until_target_selection(raw, 2)

        self.assertTrue("storage_state" in browser.context_options)
        self.assertEqual(0, len(browser.context.cookies_added))
        self.assertEqual("target_not_found", result.error_code)

    async def test_executor_maps_malformed_versioned_payload_to_existing_public_error(self):
        _, result = await self._execute_until_target_selection(
            b'{"version":2,"storage_state":{"cookies":[],"origins":[]}}',
            2,
        )

        self.assertEqual("cookie_invalid", result.error_code)

    async def test_executor_maps_playwright_invalid_location_to_cookie_invalid(self):
        _, result = await self._execute_until_target_selection(
            b'[{"name":"sid","value":"location-marker","url":"http://%/"}]',
            1,
        )

        self.assertEqual("cookie_invalid", result.error_code)

    async def test_executor_closes_browser_when_context_construction_fails(self):
        browser = _FakeBrowser(fail_new_context=True)
        raw = b'[{"name":"sid","value":"cleanup-marker","domain":".douyin.com","path":"/"}]'

        _, result = await self._execute_until_target_selection(raw, 1, browser)

        self.assertEqual("automation_failed", result.error_code)
        self.assertEqual(1, browser.close_count)


class PlaywrightLocationCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_parser_rejects_locations_rejected_by_playwright(self):
        from playwright.async_api import Error, async_playwright

        legacy_cookies = {
            "domain_percent": {
                "name": "probe",
                "value": "x",
                "domain": "%",
                "path": "/",
            },
            "url_percent": {
                "name": "probe",
                "value": "x",
                "url": "http://%/",
            },
            "domain_ipv4": {
                "name": "probe",
                "value": "x",
                "domain": "999.999.999.999",
                "path": "/",
            },
            "url_ipv4": {
                "name": "probe",
                "value": "x",
                "url": "http://999.999.999.999/",
            },
        }
        storage_cookie = {
            "name": "probe",
            "value": "x",
            "domain": "%",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }

        async with async_playwright() as playwright:
            if not Path(playwright.chromium.executable_path).exists():
                self.skipTest("Playwright Chromium binary is not installed")
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            try:
                for case, cookie in legacy_cookies.items():
                    with self.subTest(case=case):
                        playwright_rejected = False
                        try:
                            await context.add_cookies([cookie])
                        except Error:
                            playwright_rejected = True
                        self.assertTrue(playwright_rejected)
                        raw = json.dumps([cookie], separators=(",", ":")).encode()
                        with self.assertRaises(CredentialError):
                            CredentialPayload.parse(raw, 1)

                playwright_rejected = False
                try:
                    await browser.new_context(
                        storage_state={"cookies": [storage_cookie], "origins": []}
                    )
                except Error:
                    playwright_rejected = True
                self.assertTrue(playwright_rejected)
                envelope = {
                    "version": 2,
                    "storage_state": {
                        "cookies": [storage_cookie],
                        "origins": [],
                    },
                }
                raw = json.dumps(envelope, separators=(",", ":")).encode()
                with self.assertRaises(CredentialError):
                    CredentialPayload.parse(raw, 2)
            finally:
                await context.close()
                await browser.close()


if __name__ == "__main__":
    unittest.main()
