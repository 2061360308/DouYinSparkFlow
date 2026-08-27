import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from utils.logger import setup_logger


WEB_CHAT_URL = "https://www.douyin.com/chat"
CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CHAT_EDITOR_SELECTOR = ".messageEditorimChatEditorContainer"
SEARCH_INPUT_SELECTORS = (
    'input[placeholder="搜索"]',
    'input[placeholder*="搜索"]',
)

logger = setup_logger(level=logging.DEBUG)


@dataclass(frozen=True)
class DouyinUserIdentity:
    sec_uid: str
    short_id: str | None = None
    unique_id: str | None = None
    nickname: str | None = None
    remark_name: str | None = None

    @property
    def aliases(self) -> tuple[str, ...]:
        values = (
            self.remark_name,
            self.nickname,
            self.unique_id,
            self.short_id,
        )
        return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


class UserInfoCollector:
    PATH = "/aweme/v1/web/im/user/info"

    def __init__(self):
        self.identities: dict[str, DouyinUserIdentity] = {}
        self._changed = asyncio.Event()
        self._pending: set[asyncio.Task] = set()

    def capture(self, response) -> None:
        if self.PATH not in urlparse(response.url).path:
            return
        task = asyncio.create_task(self.handle_response(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def handle_response(self, response) -> None:
        if self.PATH not in urlparse(response.url).path or response.status != 200:
            return
        try:
            body = await response.json()
        except Exception:
            return
        data = body.get("data", ()) if isinstance(body, dict) else ()
        for item in data if isinstance(data, list) else ():
            if not isinstance(item, dict):
                continue
            sec_uid = str(item.get("sec_uid") or "").strip()
            if not sec_uid:
                continue
            self.identities[sec_uid] = DouyinUserIdentity(
                sec_uid=sec_uid,
                short_id=_optional_string(item.get("short_id")),
                unique_id=_optional_string(item.get("unique_id")),
                nickname=_optional_string(item.get("nickname")),
                remark_name=_optional_string(item.get("remark_name")),
            )
            self._changed.set()

    def get(self, sec_uid: str) -> DouyinUserIdentity | None:
        return self.identities.get(sec_uid)

    async def wait_for(self, sec_uid: str, timeout: float = 15.0) -> DouyinUserIdentity | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            identity = self.get(sec_uid)
            if identity is not None:
                return identity
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), deadline - loop.time())
            except TimeoutError:
                break
        return self.get(sec_uid)

    async def drain(self) -> tuple[DouyinUserIdentity, ...]:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        return tuple(self.identities.values())


def _optional_string(value) -> str | None:
    text = str(value or "").strip()
    return text or None


class TargetNotFoundError(RuntimeError):
    """Raised when the requested friend is absent from the web chat list."""


async def list_visible_web_chat_targets(page, timeout=30000):
    """Return unique, visible conversation titles from the signed-in chat list."""
    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)
    targets = []
    seen = set()
    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title and title not in seen:
            seen.add(title)
            targets.append(title)
    return targets


async def _click_search_result(result) -> None:
    try:
        send_button = result.locator(
            "xpath=ancestor::*[.//*[normalize-space()='发消息']][1]"
            "//*[normalize-space()='发消息']"
        )
        if await send_button.count() > 0 and await send_button.is_visible():
            await send_button.click()
            return
    except (AttributeError, TypeError):
        pass
    await result.click()


async def _wait_for_visible_search_results(page, candidate, timeout_ms):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(3.0, max(0.2, timeout_ms / 1000))
    while True:
        exact = page.get_by_text(candidate, exact=True)
        if await exact.count() > 0:
            results = await exact.all() if hasattr(exact, "all") else [exact.first]
            visible = []
            for result in results:
                if hasattr(result, "is_visible") and not await result.is_visible():
                    continue
                visible.append(result)
            if visible:
                return visible
        remaining = deadline - loop.time()
        if remaining <= 0:
            return []
        await asyncio.sleep(min(0.2, remaining))


async def select_web_chat_target(page, target, timeout=30000, aliases=()):
    """Select one exact target, preferring real conversation rows over page text."""
    normalized_target = target.strip()
    candidates = tuple(
        dict.fromkeys(
            value.strip() for value in (*aliases, normalized_target) if value and value.strip()
        )
    )

    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title in candidates:
            await item.click()
            return title

    for selector in SEARCH_INPUT_SELECTORS:
        try:
            search = page.locator(selector)
            if await search.count() == 0:
                continue
            field = search.first
            for candidate in candidates:
                await field.fill(candidate)
                results = await _wait_for_visible_search_results(
                    page, candidate, timeout
                )
                for result in results:
                    await _click_search_result(result)
                    return candidate
        except (AttributeError, TypeError):
            # Older page doubles and older layouts have no global search surface.
            break

    await page.wait_for_selector(CONVERSATION_ITEM_SELECTOR, timeout=timeout)

    for item in await page.locator(CONVERSATION_ITEM_SELECTOR).all():
        if hasattr(item, "is_visible") and not await item.is_visible():
            continue
        title = (
            await item.locator(CONVERSATION_TITLE_SELECTOR).inner_text()
        ).strip()
        if title in candidates:
            await item.click()
            return title

    raise TargetNotFoundError(f"未在抖音聊天列表中找到好友 {normalized_target}")


async def run_wz_web_chat_probe():
    """Send and verify one WZ message through https://www.douyin.com/chat."""
    from core.browser import get_browser
    from core.msg_builder import build_message
    from core.tasks import confirm_message_sent
    from utils.config import get_userData

    users = get_userData()
    if len(users) != 1:
        raise RuntimeError("WZ 单向测试必须且只能包含一个账号")

    user = users[0]
    targets = user.get("targets", [])
    if len(targets) != 1:
        raise RuntimeError("WZ 单向测试必须且只能包含一个目标好友")

    username = user.get("username", "未知用户")
    target = targets[0]
    playwright, browser = await get_browser()
    context = None

    try:
        context = await browser.new_context()
        context.set_default_navigation_timeout(120000)
        context.set_default_timeout(120000)
        await context.add_cookies(user["cookies"])
        page = await context.new_page()
        await page.goto(WEB_CHAT_URL)

        try:
            await select_web_chat_target(page, target)
            await page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=30000)
        except Exception:
            os.makedirs(os.path.join("logs", "diagnostics"), exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(
                path=os.path.join(
                    "logs", "diagnostics", f"web_chat_probe_{timestamp}.png"
                ),
                full_page=True,
            )
            raise

        chat_input = page.locator(CHAT_EDITOR_SELECTOR).first
        message = build_message()
        lines = message.split("\n")
        for index, line in enumerate(lines):
            await chat_input.type(line)
            if index < len(lines) - 1:
                await chat_input.press("Shift+Enter")

        logger.info(f"账号 {username} 准备通过抖音网页聊天发送消息给 {target}")
        await chat_input.press("Enter")
        await confirm_message_sent(page, chat_input, message)
        logger.info(f"账号 {username} 给好友 {target} 发送消息并确认送达完成")
    finally:
        if context is not None:
            await context.close()
        await browser.close()
        await playwright.stop()
