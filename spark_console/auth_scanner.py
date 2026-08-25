from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


CREATOR_URL = "https://creator.douyin.com/"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

LOGIN_PANEL_SELECTORS = (
    '[class*="login"]',
    '[class*="Login"]',
    '[class*="passport"]',
)
QR_SELECTORS = (
    'img[alt*="二维码"]',
    '[class*="qrcode"] img',
    '[class*="qr-code"] img',
    '[class*="qrcode"] canvas',
    '[class*="qr-code"] canvas',
)
AUTHENTICATED_SELECTOR = (
    'xpath=//*[contains(@id,"garfish_app_for_douyin_creator_pc_home")]'
)
DISPLAY_NAME_SELECTOR = (
    'xpath=//*[contains(@id,"garfish_app_for_douyin_creator_pc_home")]'
    "/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[1]/div[1]"
)
UNIQUE_ID_SELECTOR = (
    'xpath=//*[contains(@id,"garfish_app_for_douyin_creator_pc_home")]'
    "/div/div[2]/div/div[2]/div[1]/div[2]/div[1]/div[3]"
)
CONFIRMING_TEXT = ("扫码成功", "请在手机上确认", "已扫码")
VERIFICATION_TEXT = ("安全验证", "请完成验证")


class QrLoadFailed(Exception):
    pass


class LoginTimedOut(Exception):
    pass


class VerificationRequired(Exception):
    pass


class ScanCancelled(Exception):
    pass


@dataclass(frozen=True)
class ScannedAccount:
    display_name: str
    unique_id: str | None
    storage_state: dict[str, object]


def _default_playwright_factory():
    from playwright.async_api import async_playwright

    return async_playwright()


async def _invoke(callback: Callable, *args) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class DouyinQrScanner:
    def __init__(
        self,
        playwright_factory=None,
        *,
        qr_timeout_seconds: float = 45,
        login_timeout_seconds: float = 300,
        poll_interval_seconds: float = 0.2,
    ):
        self.playwright_factory = playwright_factory or _default_playwright_factory
        self.qr_timeout_seconds = qr_timeout_seconds
        self.login_timeout_seconds = login_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    async def run(
        self,
        on_qr,
        on_confirming,
        cancelled,
        *,
        expires_at: datetime | None = None,
    ) -> ScannedAccount:
        deadline = self._deadline(expires_at)
        self._checkpoint(cancelled, deadline)
        browser = None
        context = None
        async with self.playwright_factory() as playwright:
            try:
                browser = await self._await_stage(
                    playwright.chromium.launch(headless=True),
                    cancelled,
                    deadline,
                )
                context = await self._await_stage(
                    browser.new_context(), cancelled, deadline
                )
                page = await self._await_stage(
                    context.new_page(), cancelled, deadline
                )
                await self._await_stage(
                    page.goto(
                        CREATOR_URL,
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    ),
                    cancelled,
                    deadline,
                )
                qr = await self._await_stage(
                    self._find_qr(page, cancelled), cancelled, deadline
                )
                png = await self._await_stage(
                    qr.screenshot(type="png"), cancelled, deadline
                )
                if not isinstance(png, bytes) or not png.startswith(PNG_SIGNATURE):
                    raise QrLoadFailed()
                await self._await_stage(
                    _invoke(on_qr, png), cancelled, deadline
                )

                await self._await_stage(
                    self._wait_for_login(page, on_confirming, cancelled),
                    cancelled,
                    deadline,
                )
                display_name = await self._await_stage(
                    self._required_text(page, DISPLAY_NAME_SELECTOR),
                    cancelled,
                    deadline,
                )
                unique_id = await self._await_stage(
                    self._optional_text(page, UNIQUE_ID_SELECTOR),
                    cancelled,
                    deadline,
                )
                storage_state = await self._await_stage(
                    context.storage_state(), cancelled, deadline
                )
                return ScannedAccount(display_name, unique_id, storage_state)
            finally:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    if browser is not None:
                        await browser.close()

    async def _find_qr(self, page, cancelled):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.qr_timeout_seconds
        while loop.time() < deadline:
            if cancelled():
                raise ScanCancelled()
            try:
                for panel_selector in LOGIN_PANEL_SELECTORS:
                    panel = page.locator(panel_selector).first
                    if not await panel.is_visible():
                        continue
                    for qr_selector in QR_SELECTORS:
                        qr = panel.locator(qr_selector).first
                        if await qr.is_visible():
                            return qr
                for qr in await page.locator("img, canvas").all():
                    if not await qr.is_visible():
                        continue
                    box = await qr.bounding_box()
                    if box is None:
                        continue
                    width, height = box["width"], box["height"]
                    if not (150 <= width <= 220 and 150 <= height <= 220):
                        continue
                    if abs(width - height) > 8:
                        continue
                    if await qr.evaluate(
                        'element => Boolean(element.closest("[class*=feature]"))'
                    ):
                        continue
                    return qr
            except ScanCancelled:
                raise
            except Exception:
                pass
            await asyncio.sleep(self.poll_interval_seconds)
        raise QrLoadFailed()

    async def _wait_for_login(self, page, on_confirming, cancelled) -> None:
        authenticated = asyncio.create_task(
            page.wait_for_selector(
                AUTHENTICATED_SELECTOR, state="visible", timeout=0
            )
        )
        confirming = asyncio.create_task(
            self._wait_for_any_text(page, CONFIRMING_TEXT)
        )
        verification = asyncio.create_task(
            self._wait_for_any_text(page, VERIFICATION_TEXT)
        )
        cancellation = asyncio.create_task(self._wait_until_cancelled(cancelled))
        pending = {authenticated, confirming, verification, cancellation}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                if cancellation in done:
                    cancellation.result()
                    raise ScanCancelled()
                if verification in done:
                    verification.result()
                    raise VerificationRequired()
                if confirming in done:
                    confirming.result()
                    await _invoke(on_confirming, True)
                if authenticated in done:
                    authenticated.result()
                    return
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    async def _wait_for_any_text(page, values: tuple[str, ...]) -> None:
        tasks = {
            asyncio.create_task(
                page.wait_for_selector(
                    f"text={value}", state="visible", timeout=0
                )
            )
            for value in values
        }
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            first = next(iter(done))
            first.result()
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_until_cancelled(self, cancelled) -> None:
        while True:
            if cancelled():
                return
            await asyncio.sleep(self.poll_interval_seconds)

    def _deadline(self, expires_at: datetime | None) -> float:
        if expires_at is None:
            remaining = self.login_timeout_seconds
        else:
            value = expires_at
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            remaining = (value - datetime.now(timezone.utc)).total_seconds()
        return asyncio.get_running_loop().time() + max(0, remaining)

    @staticmethod
    def _checkpoint(cancelled, deadline: float) -> None:
        if cancelled():
            raise ScanCancelled()
        if asyncio.get_running_loop().time() >= deadline:
            raise LoginTimedOut()

    async def _await_stage(self, awaitable, cancelled, deadline: float):
        try:
            self._checkpoint(cancelled, deadline)
        except BaseException:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            elif isinstance(awaitable, asyncio.Future):
                awaitable.cancel()
            raise
        operation = asyncio.create_task(awaitable)
        cancellation = asyncio.create_task(self._wait_until_cancelled(cancelled))
        timeout = asyncio.create_task(
            asyncio.sleep(
                max(0, deadline - asyncio.get_running_loop().time())
            )
        )
        watchers = {cancellation, timeout}
        try:
            done, _pending = await asyncio.wait(
                {operation, *watchers}, return_when=asyncio.FIRST_COMPLETED
            )
            if operation in done or operation.done():
                return operation.result()
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            if cancellation in done:
                cancellation.result()
                raise ScanCancelled()
            timeout.result()
            raise LoginTimedOut()
        finally:
            if not operation.done():
                operation.cancel()
            for task in watchers:
                task.cancel()
            await asyncio.gather(operation, *watchers, return_exceptions=True)

    @staticmethod
    async def _required_text(page, selector: str) -> str:
        text = (await page.locator(selector).first.inner_text(timeout=5_000)).strip()
        if not text:
            raise RuntimeError("account identity unavailable")
        return text

    @staticmethod
    async def _optional_text(page, selector: str) -> str | None:
        locator = page.locator(selector).first
        try:
            if not await locator.is_visible():
                return None
            text = (await locator.inner_text(timeout=5_000)).strip()
        except Exception:
            return None
        for prefix in ("抖音号：", "抖音号:"):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        return text or None
