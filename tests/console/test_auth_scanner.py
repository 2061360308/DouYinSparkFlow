import asyncio
import gc
import unittest
import warnings
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from spark_console.auth_scanner import (
    AUTHENTICATED_SELECTOR,
    CHAT_AUTHENTICATED_SELECTOR,
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
from core.web_chat import CONVERSATION_ITEM_SELECTOR, CONVERSATION_TITLE_SELECTOR


PNG = b"\x89PNG\r\n\x1a\nscanner-fixture"
PAGE_PNG = b"\x89PNG\r\n\x1a\nfull-browser-view"


class _Locator:
    def __init__(self, *, visible=False, visible_sequence=None, png=None, text="", children=None, items=None, box=None, decorative=False, on_click=None):
        self.visible = visible
        self.visible_sequence = list(visible_sequence or [])
        self.png = png
        self.text = text
        self.children = children or {}
        self.items = items or []
        self.box = box
        self.decorative = decorative
        self.on_click = on_click
        self.clicked = False

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self.children.get(selector, _Locator())

    async def is_visible(self, **_kwargs):
        if self.visible_sequence:
            self.visible = self.visible_sequence.pop(0)
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

    async def click(self, **_kwargs):
        self.clicked = True
        if self.on_click is not None:
            self.on_click()


class _Response:
    def __init__(self, status):
        self.url = "https://creator.douyin.com/passport/web/check_qrconnect/"
        self._status = status

    async def json(self):
        return {"data": {"status": self._status, "error_code": 0}}


class _Page:
    def __init__(self, mode="success", *, qr_visible=True, semantic_qr=False, normal_verification_tab=False, profile_visible=True):
        self.mode = mode
        self.url = "https://creator.douyin.com/"
        self.authenticated = asyncio.Event()
        self.never = asyncio.Event()
        self.navigation_started = asyncio.Event()
        self.qr = _Locator(
            visible=False if mode in {"chat_entry", "delayed_chat_entry"} else qr_visible,
            visible_sequence=[True, True, False]
            if mode in {"credential_success", "cookie_only"}
            else None,
            png=PNG,
        )
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
        self.profile_visible = profile_visible
        self.listeners = {}
        self.goto_url = None
        self.login_button = _Locator(
            visible=True,
            visible_sequence=[False, False, True]
            if mode == "delayed_chat_entry"
            else None,
            on_click=lambda: setattr(self.qr, "visible", bool(qr_visible)),
        )
        self.sms_button = _Locator(visible=mode == "sms_verification")
        self.verify_button = _Locator(visible=mode == "sms_verification")
        self.viewport_size = {"width": 1280, "height": 720}
        self.keyboard = _Keyboard()

    async def screenshot(self, **_kwargs):
        return PAGE_PNG

    async def goto(self, *_args, **_kwargs):
        self.goto_url = _args[0]
        self.navigation_started.set()
        if self.mode == "navigation":
            await self.never.wait()
        return None

    def get_by_text(self, text, exact=False):
        if exact and text == "登录":
            return _Locator(items=[self.login_button])
        if exact and text == "接收短信验证码":
            return _Locator(items=[self.sms_button])
        if exact and text == "验证":
            return _Locator(items=[self.verify_button])
        return _Locator(items=[])

    def locator(self, selector):
        if selector == "img, canvas":
            return self.semantic_candidates
        if selector == LOGIN_PANEL_SELECTORS[0]:
            return self.panel
        if selector == DISPLAY_NAME_SELECTOR:
            return _Locator(visible=self.profile_visible, text=" 测试昵称 " if self.profile_visible else "")
        if selector == UNIQUE_ID_SELECTOR:
            return _Locator(visible=True, text=" 抖音号：douyin-123 ")
        if selector == CONVERSATION_ITEM_SELECTOR and self.mode == "chat_dom_success":
            return _Locator(
                items=[
                    _Locator(
                        visible=True,
                        children={CONVERSATION_TITLE_SELECTOR: _Locator(text=" wzlovegsy ")},
                    ),
                    _Locator(
                        visible=True,
                        children={CONVERSATION_TITLE_SELECTOR: _Locator(text="gsy")},
                    ),
                ]
            )
        return _Locator()

    def on(self, event, listener):
        self.listeners[event] = listener
        if event == "response" and self.mode == "network_success":
            loop = asyncio.get_running_loop()
            loop.call_soon(listener, _Response("scanned"))
            loop.call_soon(listener, _Response("confirmed"))

    def remove_listener(self, event, listener):
        if self.listeners.get(event) is listener:
            self.listeners.pop(event)

    async def wait_for_selector(self, selector, **_kwargs):
        if selector == "text=验证码" and self.normal_verification_tab:
            return _Locator(visible=True, text="验证码登录")
        if selector == AUTHENTICATED_SELECTOR:
            await self.authenticated.wait()
            return _Locator(visible=True)
        if selector == CHAT_AUTHENTICATED_SELECTOR:
            if self.mode == "chat_dom_success":
                await asyncio.sleep(0)
                return _Locator(visible=True)
            await self.never.wait()
        if any(text in selector for text in CONFIRMING_TEXT):
            if self.mode in {"success", "url_success", "chat_entry", "delayed_chat_entry", "chat_account_success"}:
                await asyncio.sleep(0)
                if self.mode == "success":
                    asyncio.get_running_loop().call_soon(self.authenticated.set)
                elif self.mode != "chat_account_success":
                    self.url = "https://creator.douyin.com/creator-micro/home"
                return _Locator(visible=True)
            await self.never.wait()
        if any(text in selector for text in VERIFICATION_TEXT):
            if self.mode == "verification":
                await asyncio.sleep(0)
                return _Locator(visible=True)
            await self.never.wait()
        await self.never.wait()


class _Context:
    def __init__(self, page, *, fail_storage_state=False, credential_success=False):
        self.page = page
        self.fail_storage_state = fail_storage_state
        self.closed = False
        self.credential_success = credential_success
        self.cookie_reads = 0
        self.request = _ContextRequest(
            authenticated=page.mode
            in {
                "success",
                "url_success",
                "chat_entry",
                "delayed_chat_entry",
                "chat_account_success",
                "credential_success",
            }
        )

    async def new_page(self):
        return self.page

    async def storage_state(self):
        if self.fail_storage_state:
            raise RuntimeError("fixed storage failure")
        cookies = [
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
            ]
        if self.credential_success:
            cookies.append(
                {
                    "name": "auth-session",
                    "value": "authenticated-marker",
                    "domain": ".douyin.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        return {
            "cookies": cookies,
            "origins": [],
        }

    async def cookies(self):
        self.cookie_reads += 1
        state = await self.storage_state()
        if self.credential_success and self.cookie_reads == 1:
            return state["cookies"][:1]
        return state["cookies"]

    async def close(self):
        self.closed = True


class _ContextResponse:
    def __init__(self, authenticated):
        self.authenticated = authenticated

    async def json(self):
        return {
            "message": "success" if self.authenticated else "error",
            "data": {
                "error_code": 0 if self.authenticated else 13,
                "user_id": "user-fixture" if self.authenticated else "",
            },
        }


class _ContextRequest:
    def __init__(self, authenticated=False):
        self.authenticated = authenticated

    async def get(self, _url, **_kwargs):
        return _ContextResponse(self.authenticated)


class _Keyboard:
    def __init__(self):
        self.values = []

    async def type(self, value):
        self.values.append(value)


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
        profile_visible=True,
        fail_storage_state=False,
        qr_timeout_seconds=0.01,
        login_timeout_seconds=0.2,
    ):
        page = _Page(
            mode,
            qr_visible=qr_visible,
            semantic_qr=semantic_qr,
            normal_verification_tab=normal_verification_tab,
            profile_visible=profile_visible,
        )
        context = _Context(
            page,
            fail_storage_state=fail_storage_state,
            credential_success=mode in {"credential_success", "cookie_only"},
        )
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

        self.assertGreaterEqual(len(qr_images), 1)
        self.assertTrue(all(image == PAGE_PNG for image in qr_images))
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
        self.assertGreaterEqual(len(qr_images), 1)
        self.assertTrue(all(image == PAGE_PNG for image in qr_images))
        self.assertEqual([True], confirmations)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_chat_login_entry_is_opened_before_qr_capture(self):
        scanner, browser, context = self._scanner(mode="chat_entry")

        result = await scanner.run(
            lambda _png: None,
            lambda _confirmed: None,
            lambda: False,
        )

        self.assertEqual("https://www.douyin.com/chat", context.page.goto_url)
        self.assertTrue(context.page.login_button.clicked)
        self.assertEqual("测试昵称", result.display_name)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_initial_image_contains_the_complete_cloud_browser_view(self):
        scanner, browser, context = self._scanner(mode="chat_entry")
        views = []

        await scanner.run(views.append, lambda _confirmed: None, lambda: False)

        self.assertEqual(PAGE_PNG, views[0])
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_confirmed_chat_login_completes_from_account_api_without_url_change(self):
        scanner, browser, context = self._scanner(
            mode="chat_account_success", profile_visible=False
        )

        result = await scanner.run(
            lambda _png: None,
            lambda _confirmed: None,
            lambda: False,
        )

        self.assertEqual("https://creator.douyin.com/", context.page.url)
        self.assertEqual("抖音账号", result.display_name)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_loaded_chat_conversation_list_completes_without_account_api(self):
        scanner, browser, context = self._scanner(
            mode="chat_dom_success", profile_visible=False
        )

        result = await scanner.run(
            lambda _png: None,
            lambda _confirmed: None,
            lambda: False,
        )

        self.assertEqual("抖音账号", result.display_name)
        self.assertTrue(result.storage_state["cookies"])
        self.assertEqual(("wzlovegsy", "gsy"), result.conversation_names)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_cloud_browser_stream_types_claimed_verification_code(self):
        scanner, _browser, context = self._scanner(mode="timeout")
        actions = iter(({"kind": "text", "text": "123456"}, None))

        async def stop_after_view(_png):
            raise RuntimeError("view captured")

        with self.assertRaisesRegex(RuntimeError, "view captured"):
            await scanner._stream_browser_view(
                context.page, stop_after_view, lambda: next(actions)
            )

        self.assertEqual(["123456"], context.page.keyboard.values)

    async def test_verification_code_is_submitted_after_two_seconds(self):
        scanner, _browser, context = self._scanner(mode="sms_verification")

        with patch(
            "spark_console.auth_scanner.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            submitted = await scanner._type_and_submit_verification_code(
                context.page, "123456"
            )

        self.assertTrue(submitted)
        self.assertEqual(["123456"], context.page.keyboard.values)
        sleep.assert_awaited_once_with(2)
        self.assertTrue(context.page.verify_button.clicked)

    async def test_cloud_browser_prefers_sms_verification_when_option_appears(self):
        scanner, _browser, context = self._scanner(mode="sms_verification")

        self.assertTrue(
            hasattr(scanner, "_select_sms_verification"),
            "scanner must support selecting SMS verification",
        )
        selected = await scanner._select_sms_verification(context.page)

        self.assertTrue(selected)
        self.assertTrue(context.page.sms_button.clicked)

    async def test_chat_login_entry_waits_for_delayed_login_button(self):
        scanner, browser, context = self._scanner(
            mode="delayed_chat_entry", qr_timeout_seconds=0.05
        )

        result = await scanner.run(
            lambda _png: None,
            lambda _confirmed: None,
            lambda: False,
        )

        self.assertTrue(context.page.login_button.clicked)
        self.assertEqual("测试昵称", result.display_name)
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_authenticated_creator_url_completes_when_legacy_home_dom_is_absent(self):
        scanner, browser, context = self._scanner(
            mode="url_success", profile_visible=False
        )

        result = await scanner.run(
            lambda _png: None,
            lambda _confirmed: None,
            lambda: False,
        )

        self.assertEqual("抖音账号", result.display_name)
        self.assertTrue(result.storage_state["cookies"])
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_hidden_qr_and_new_http_only_cookie_complete_without_legacy_dom(self):
        scanner, browser, context = self._scanner(
            mode="credential_success", profile_visible=False
        )
        confirmations = []

        result = await scanner.run(
            lambda _png: None,
            confirmations.append,
            lambda: False,
        )

        self.assertEqual("抖音账号", result.display_name)
        self.assertEqual([True], confirmations)
        self.assertEqual(2, len(result.storage_state["cookies"]))
        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_cookie_change_alone_does_not_save_unauthenticated_state(self):
        scanner, browser, context = self._scanner(
            mode="cookie_only",
            profile_visible=False,
            login_timeout_seconds=0.2,
        )

        with self.assertRaises(LoginTimedOut):
            await scanner.run(
                lambda _png: None,
                lambda _confirmed: None,
                lambda: False,
            )

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)

    async def test_qrconnect_confirmation_does_not_save_unauthenticated_state(self):
        scanner, browser, context = self._scanner(
            mode="network_success",
            profile_visible=False,
            login_timeout_seconds=0.2,
        )
        confirmations = []

        with self.assertLogs("spark_console.auth_scanner", level="INFO") as logs:
            with self.assertRaises(LoginTimedOut):
                await scanner.run(
                    lambda _png: None,
                    confirmations.append,
                    lambda: False,
                )

        self.assertEqual([True], confirmations)
        self.assertTrue(any("status=scanned" in line for line in logs.output))
        self.assertTrue(any("status=confirmed" in line for line in logs.output))
        self.assertNotIn("response", context.page.listeners)
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
