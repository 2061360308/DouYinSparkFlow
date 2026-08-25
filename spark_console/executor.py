from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from core.web_chat import CHAT_EDITOR_SELECTOR, WEB_CHAT_URL, TargetNotFoundError, select_web_chat_target


class ExecutionStage(StrEnum):
    STARTING = "starting"
    AUTHENTICATING = "authenticating"
    SELECTING_TARGET = "selecting_target"
    SENDING = "sending"
    CONFIRMING = "confirming"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    stage: str
    error_code: str | None = None
    error_summary: str | None = None


class DouyinExecutor:
    async def execute(self, cookie_payload: bytes | bytearray, target: str, message: str) -> ExecutionResult:
        from playwright.async_api import async_playwright
        from core.tasks import confirm_message_sent

        try:
            cookies = json.loads(bytes(cookie_payload))
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                context = await browser.new_context()
                try:
                    await context.add_cookies(cookies)
                    page = await context.new_page()
                    await page.goto(WEB_CHAT_URL, wait_until="domcontentloaded", timeout=120000)
                    await select_web_chat_target(page, target, timeout=45000)
                    await page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=30000)
                    editor = page.locator(CHAT_EDITOR_SELECTOR).first
                    lines = message.splitlines() or [message]
                    for index, line in enumerate(lines):
                        await editor.type(line)
                        if index < len(lines) - 1:
                            await editor.press("Shift+Enter")
                    await editor.press("Enter")
                    await confirm_message_sent(page, editor, message, timeout=20000)
                    return ExecutionResult(True, ExecutionStage.COMPLETE)
                finally:
                    await context.close()
                    await browser.close()
        except TargetNotFoundError:
            return ExecutionResult(False, ExecutionStage.SELECTING_TARGET, "target_not_found", "未找到完全匹配的目标好友")
        except json.JSONDecodeError:
            return ExecutionResult(False, ExecutionStage.AUTHENTICATING, "cookie_invalid", "账号凭据格式无效")
        except Exception:
            return ExecutionResult(False, ExecutionStage.CONFIRMING, "automation_failed", "页面操作或发送确认失败")
