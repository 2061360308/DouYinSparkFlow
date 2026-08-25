import asyncio
import unittest

from spark_console.auth_scanner import (
    AUTHENTICATED_SELECTOR,
    CONFIRMING_TEXT,
    DISPLAY_NAME_SELECTOR,
    LOGIN_PANEL_SELECTORS,
    QR_SELECTORS,
    UNIQUE_ID_SELECTOR,
    VERIFICATION_TEXT,
    DouyinQrScanner,
    LoginTimedOut,
    QrLoadFailed,
    ScanCancelled,
    VerificationRequired,
)


PNG = b"\x89PNG\r\n\x1a\nscanner-fixture"


class _Locator:
    def __init__(self, *, visible=False, png=None, text="", children=None):
        self.visible = visible
        self.png = png
        self.text = text
        self.children = children or {}

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self.children.get(selector, _Locator())

    async def is_visible(self, **_kwargs):
        return self.visible

    async def screenshot(self, **_kwargs):
        return self.png

    async def inner_text(self, **_kwargs):
        return self.text


class _Page:
    def __init__(self, mode="success", *, qr_visible=True):
        self.mode = mode
        self.authenticated = asyncio.Event()
        self.never = asyncio.Event()
        self.qr = _Locator(visible=qr_visible, png=PNG)
        self.panel = _Locator(
            visible=True,
            children={selector: self.qr for selector in QR_SELECTORS},
        )

    async def goto(self, *_args, **_kwargs):
        return None

    def locator(self, selector):
        if selector == LOGIN_PANEL_SELECTORS[0]:
            return self.panel
        if selector == DISPLAY_NAME_SELECTOR:
            return _Locator(visible=True, text=" 测试昵称 ")
        if selector == UNIQUE_ID_SELECTOR:
            return _Locator(visible=True, text=" 抖音号：douyin-123 ")
        return _Locator()

    async def wait_for_selector(self, selector, **_kwargs):
        if selector == AUTHENTICATED_SELECTOR:
            await self.authenticated.wait()
            return _Locator(visible=True)
        if any(text in selector for text in CONFIRMING_TEXT):
            if self.mode == "success":
                await asyncio.sleep(0)
                asyncio.get_running_loop().call_soon(self.authenticated.set)
                return _Locator(visible=True)
            await self.never.wait()
        if any(text in selector for text in VERIFICATION_TEXT):
            if self.mode == "verification":
                await asyncio.sleep(0)
                return _Locator(visible=True)
            await self.never.wait()
        await self.never.wait()


class _Context:
    def __init__(self, page, *, fail_storage_state=False):
        self.page = page
        self.fail_storage_state = fail_storage_state
        self.closed = False

    async def new_page(self):
        return self.page

    async def storage_state(self):
        if self.fail_storage_state:
            raise RuntimeError("fixed storage failure")
        return {
            "cookies": [
                {
                    "name": "sid",
                    "value": "scanner-cookie-marker",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }

    async def close(self):
        self.closed = True


class _Browser:
    def __init__(self, context, *, fail_new_context=False):
        self.context = context
        self.fail_new_context = fail_new_context
        self.closed = False

    async def new_context(self):
        if self.fail_new_context:
            raise RuntimeError("fixed context failure")
        return self.context

    async def close(self):
        self.closed = True


class _Chromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **_kwargs):
        return self.browser


class _PlaywrightManager:
    def __init__(self, browser):
        self.playwright = type(
            "FakePlaywright", (), {"chromium": _Chromium(browser)}
        )()

    async def __aenter__(self):
        return self.playwright

    async def __aexit__(self, *_args):
        return None


class DouyinQrScannerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def _scanner(self, mode="success", *, qr_visible=True, fail_storage_state=False):
        page = _Page(mode, qr_visible=qr_visible)
        context = _Context(page, fail_storage_state=fail_storage_state)
        browser = _Browser(context)
        scanner = DouyinQrScanner(
            playwright_factory=lambda: _PlaywrightManager(browser),
            qr_timeout_seconds=0.01,
            login_timeout_seconds=0.03,
            poll_interval_seconds=0,
        )
        return scanner, browser, context

    async def test_scanner_returns_account_after_qr_and_mobile_confirmation(self):
        scanner, browser, context = self._scanner()
        qr_images = []
        confirmations = []

        result = await scanner.run(
            qr_images.append,
            confirmations.append,
            lambda: False,
        )

        self.assertEqual("测试昵称", result.display_name)
        self.assertEqual("douyin-123", result.unique_id)
        self.assertEqual(1, len(result.storage_state["cookies"]))
        self.assertEqual(1, len(qr_images))
        self.assertTrue(qr_images[0].startswith(b"\x89PNG"))
        self.assertEqual([True], confirmations)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_missing_qr_maps_to_qr_load_failed_and_closes_resources(self):
        scanner, browser, context = self._scanner(qr_visible=False)

        with self.assertRaises(QrLoadFailed):
            await scanner.run(lambda _png: None, lambda: None, lambda: False)

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_login_deadline_maps_to_login_timed_out(self):
        scanner, browser, context = self._scanner(mode="timeout")

        with self.assertRaises(LoginTimedOut):
            await scanner.run(lambda _png: None, lambda: None, lambda: False)

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_extra_verification_is_not_bypassed(self):
        scanner, browser, context = self._scanner(mode="verification")

        with self.assertRaises(VerificationRequired):
            await scanner.run(lambda _png: None, lambda: None, lambda: False)

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_cancellation_stops_scan_and_closes_resources(self):
        scanner, browser, context = self._scanner(mode="timeout")

        with self.assertRaises(ScanCancelled):
            await scanner.run(lambda _png: None, lambda: None, lambda: True)

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_unexpected_storage_failure_still_closes_context_and_browser(self):
        scanner, browser, context = self._scanner(fail_storage_state=True)

        with self.assertRaises(RuntimeError):
            await scanner.run(
                lambda _png: None, lambda _confirmed: None, lambda: False
            )

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_browser_closes_when_context_creation_fails(self):
        page = _Page()
        context = _Context(page)
        browser = _Browser(context, fail_new_context=True)
        scanner = DouyinQrScanner(
            playwright_factory=lambda: _PlaywrightManager(browser),
            qr_timeout_seconds=0.01,
            login_timeout_seconds=0.03,
            poll_interval_seconds=0,
        )

        with self.assertRaises(RuntimeError):
            await scanner.run(lambda _png: None, lambda: None, lambda: False)

        self.assertTrue(browser.closed)


if __name__ == "__main__":
    unittest.main()
