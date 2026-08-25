import unittest
from types import ModuleType
from unittest.mock import patch

from spark_console.credentials import CredentialError, CredentialPayload
from spark_console.executor import DouyinExecutor


class CredentialPayloadTests(unittest.TestCase):
    def test_version_one_cookie_array_is_added_after_context_creation(self):
        raw = b'[{"name":"sid","value":"legacy-secret","domain":".douyin.com","path":"/"}]'

        payload = CredentialPayload.parse(raw, 1)

        self.assertEqual({}, payload.context_options())
        self.assertTrue(payload.cookies_to_add()[0]["value"] == "legacy-secret")

    def test_version_two_storage_state_becomes_context_option(self):
        raw = b'{"version":2,"storage_state":{"cookies":[{"name":"sid","value":"secret","domain":".douyin.com","path":"/"}],"origins":[]}}'

        payload = CredentialPayload.parse(raw, 2)

        self.assertTrue(
            payload.context_options()["storage_state"]["cookies"][0]["value"]
            == "secret"
        )
        self.assertEqual([], payload.cookies_to_add())

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

        for raw, version in fixtures:
            with self.subTest(version=version):
                with self.assertRaises(CredentialError) as caught:
                    CredentialPayload.parse(raw, version)
                self.assertFalse("marker-secret" in str(caught.exception))


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
    def __init__(self):
        self.context_options = None
        self.context = _FakeContext()

    async def new_context(self, **options):
        self.context_options = options
        return self.context

    async def close(self):
        return None


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
    async def _execute_until_target_selection(self, raw, version):
        from core.web_chat import TargetNotFoundError

        browser = _FakeBrowser()
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

        self.assertFalse(browser.context_options)
        self.assertEqual(1, len(browser.context.cookies_added))
        self.assertEqual("target_not_found", result.error_code)

    async def test_executor_creates_version_two_context_without_adding_cookies(self):
        raw = b'{"version":2,"storage_state":{"cookies":[{"name":"sid","value":"storage-executor-marker","domain":".douyin.com","path":"/"}],"origins":[]}}'

        browser, result = await self._execute_until_target_selection(raw, 2)

        self.assertTrue("storage_state" in browser.context_options)
        self.assertFalse(browser.context.cookies_added)
        self.assertEqual("target_not_found", result.error_code)

    async def test_executor_maps_malformed_versioned_payload_to_existing_public_error(self):
        _, result = await self._execute_until_target_selection(
            b'{"version":2,"storage_state":{"cookies":[],"origins":[]}}',
            2,
        )

        self.assertEqual("cookie_invalid", result.error_code)


if __name__ == "__main__":
    unittest.main()
