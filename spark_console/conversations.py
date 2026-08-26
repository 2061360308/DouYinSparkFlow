from __future__ import annotations

from core.web_chat import WEB_CHAT_URL, list_visible_web_chat_targets
from spark_console.credentials import CredentialPayload


async def discover_conversations(
    cookie_payload: bytes | bytearray, credential_version: int
) -> list[str]:
    """Read conversation names visible to an already-bound Douyin account."""
    from playwright.async_api import async_playwright

    payload = CredentialPayload.parse(bytes(cookie_payload), credential_version)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = None
        try:
            context = await browser.new_context(**payload.context_options())
            legacy_cookies = payload.cookies_to_add()
            if legacy_cookies:
                await context.add_cookies(legacy_cookies)
            page = await context.new_page()
            await page.goto(
                WEB_CHAT_URL, wait_until="domcontentloaded", timeout=120_000
            )
            return await list_visible_web_chat_targets(page, timeout=45_000)
        finally:
            try:
                if context is not None:
                    await context.close()
            finally:
                await browser.close()
