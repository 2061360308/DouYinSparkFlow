import asyncio
import gc
import unittest
import warnings
from datetime import datetime, timedelta, timezone

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
    def __init__(self, *, visible=False, png=None, text="", children=None, items=None, box=None, decorative=False):
        self.visible = visible
        self.png = png
        self.text = text
        self.children = children or {}
        self.items = items or []
        self.box = box
        self.decorative = decorative

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

    async def all(self):
        return self.items

    async def bounding_box(self):
        return self.box

    async def evaluate(self, _expression):
        return self.decorative


class _Page:
    def __init__(self, mode="success", *, qr_visible=True, semantic_qr=False, normal_verification_tab=False):
        self.mode = mode
        self.authenticated = asyncio.Event()
        self.never = asyncio.Event()
        self.navigation_started = asyncio.Event()
        self.qr = _Locator(visible=qr_visible, png=PNG)
        self.panel = _Locator(
            visible=True,
            children={} if semantic_qr else {selector: self.qr for selector in QR_SELECTORS},
        )
        self.semantic_candidates = _Locator(
            items=[
                _Locator(visible=True, png=PNG, box={"width": 180, "height": 180}, decorative=True),
                _Locator(visible=True, png=PNG, box={"width": 178, "height": 178}),
            ]
            if semantic_qr
            else []
        )
        self.normal_verification_tab = normal_verification_tab

    async def goto(self, *_args, **_kwargs):
        self.navigation_started.set()
        if self.mode == "navigation":
            await self.never.wait()
        return None

    def locator(self, selector):
        if selector == "img, canvas":
            return self.semantic_candidates
        if selector == LOGIN_PANEL_SELECTORS[0]:
            return self.panel
        if selector == DISPLAY_NAME_SELECTOR:
            return _Locator(visible=True, text=" 测试昵称 ")
        if selector == UNIQUE_ID_SELECTOR:
            return _Locator(visible=True, text=" 抖音号：douyin-123 ")
        return _Locator()

    async def wait_for_selector(self, selector, **_kwargs):
        if selector == "text=验证码" and self.normal_verification_tab:
            return _Locator(visible=True, text="验证码登录")
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

    def _scanner(
        self,
        mode="success",
        *,
        qr_visible=True,
        semantic_qr=False,
        normal_verification_tab=False,
        fail_storage_state=False,
        qr_timeout_seconds=0.01,
        login_timeout_seconds=0.2,
    ):
        page = _Page(
            mode,
            qr_visible=qr_visible,
            semantic_qr=semantic_qr,
            normal_verification_tab=normal_verification_tab,
        )
        context = _Context(page, fail_storage_state=fail_storage_state)
        browser = _Browser(context)
        scanner = DouyinQrScanner(
            playwright_factory=lambda: _PlaywrightManager(browser),
            qr_timeout_seconds=qr_timeout_seconds,
            login_timeout_seconds=login_timeout_seconds,
            poll_interval_seconds=0,
        )
        return scanner, browser, context

    async def test_randomized_square_qr_is_found_without_qrcode_class(self):
        scanner, browser, context = self._scanner(semantic_qr=True)
        qr_images = []

        try:
            await scanner.run(qr_images.append, lambda _value: None, lambda: False)
        except QrLoadFailed:
            self.fail("randomized square QR should be discovered")

        self.assertEqual([PNG], qr_images)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_normal_verification_login_tab_is_not_extra_verification(self):
        scanner, _browser, context = self._scanner(
            mode="timeout", normal_verification_tab=True
        )
        checks = 0

        def cancelled():
            nonlocal checks
            checks += 1
            return checks > 10

        try:
            with self.assertRaises(ScanCancelled):
                await scanner._wait_for_login(
                    context.page, lambda _value: None, cancelled
                )
        except VerificationRequired:
            self.fail("normal verification-code login tab must not block QR login")

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
        scanner, browser, context = self._scanner(
            mode="timeout", login_timeout_seconds=0.03
        )

        with self.assertRaises(LoginTimedOut):
            await scanner.run(lambda _png: None, lambda: None, lambda: False)

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_persisted_deadline_interrupts_navigation(self):
        scanner, browser, context = self._scanner(
            mode="navigation", login_timeout_seconds=1
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=0.03)

        with self.assertRaises(LoginTimedOut):
            await asyncio.wait_for(
                scanner.run(
                    lambda _png: None,
                    lambda _confirmed: None,
                    lambda: False,
                    expires_at=expires_at,
                ),
                timeout=0.2,
            )

        self.assertTrue(context.page.navigation_started.is_set())
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_persisted_deadline_caps_qr_loading(self):
        scanner, browser, context = self._scanner(
            mode="timeout",
            qr_visible=False,
            qr_timeout_seconds=1,
            login_timeout_seconds=1,
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=0.03)

        with self.assertRaises(LoginTimedOut):
            await asyncio.wait_for(
                scanner.run(
                    lambda _png: None,
                    lambda _confirmed: None,
                    lambda: False,
                    expires_at=expires_at,
                ),
                timeout=0.2,
            )

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_persisted_deadline_caps_login_wait(self):
        scanner, browser, context = self._scanner(
            mode="timeout", login_timeout_seconds=1
        )
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=0.03)

        with self.assertRaises(LoginTimedOut):
            await asyncio.wait_for(
                scanner.run(
                    lambda _png: None,
                    lambda _confirmed: None,
                    lambda: False,
                    expires_at=expires_at,
                ),
                timeout=0.2,
            )

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_expired_checkpoint_closes_unstarted_stage_awaitable(self):
        scanner, _browser, _context = self._scanner()
        awaitable = asyncio.sleep(0)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            with self.assertRaises(LoginTimedOut):
                await scanner._await_stage(
                    awaitable,
                    lambda: False,
                    asyncio.get_running_loop().time() - 1,
                )
            del awaitable
            gc.collect()

        self.assertFalse(
            any(issubclass(item.category, RuntimeWarning) for item in caught)
        )

    async def test_extra_verification_is_not_bypassed(self):
        scanner, browser, context = self._scanner(mode="verification")

        with self.assertRaises(VerificationRequired):
            await scanner.run(lambda _png: None, lambda: None, lambda: False)

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_cancellation_before_launch_stops_scan(self):
        scanner, browser, context = self._scanner(mode="timeout")

        with self.assertRaises(ScanCancelled):
            await scanner.run(lambda _png: None, lambda: None, lambda: True)

    async def test_late_cancellation_interrupts_navigation_and_closes_resources(self):
        scanner, browser, context = self._scanner(
            mode="navigation", login_timeout_seconds=1
        )
        stopping = asyncio.Event()
        task = asyncio.create_task(
            scanner.run(
                lambda _png: None,
                lambda _confirmed: None,
                stopping.is_set,
            )
        )
        await asyncio.wait_for(context.page.navigation_started.wait(), timeout=0.1)

        stopping.set()

        with self.assertRaises(ScanCancelled):
            await asyncio.wait_for(task, timeout=0.2)
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
