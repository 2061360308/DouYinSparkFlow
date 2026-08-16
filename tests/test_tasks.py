import unittest

from core.tasks import (
    click_first_match,
    confirm_message_sent,
    page_has_login_prompt,
    wait_for_first_visible,
)


class FakePage:
    def __init__(self, visible_selector):
        self.visible_selector = visible_selector
        self.attempts = []

    async def wait_for_selector(self, selector, *, state, timeout):
        self.attempts.append((selector, state, timeout))
        if selector != self.visible_selector:
            raise TimeoutError(selector)
        return selector


class FakeLocator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count


class FakeLoginPage:
    def __init__(self, visible_text, url="https://creator.douyin.com/creator-micro/data/following/chat"):
        self.visible_text = visible_text
        self.url = url

    def locator(self, selector):
        return FakeLocator(1 if selector == f"text={self.visible_text}" else 0)


class FakeClickableLocator:
    def __init__(self):
        self.first = self
        self.clicked = False

    async def click(self):
        self.clicked = True


class FakeClickablePage:
    def __init__(self):
        self.result = FakeClickableLocator()

    def locator(self, selector):
        return self.result


class FakeMessageLocator:
    def __init__(self):
        self.last = self
        self.wait_calls = []

    async def wait_for(self, *, state, timeout):
        self.wait_calls.append((state, timeout))


class FakeChatInput:
    async def element_handle(self):
        return "chat-input-handle"


class FakeDeliveryPage:
    def __init__(self):
        self.wait_function_calls = []
        self.message_locator = FakeMessageLocator()
        self.requested_text = None

    async def wait_for_function(self, expression, element, *, timeout):
        self.wait_function_calls.append((expression, element, timeout))

    def get_by_text(self, text, *, exact):
        self.requested_text = (text, exact)
        return self.message_locator


class WaitForFirstVisibleTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_the_first_visible_selector(self):
        page = FakePage("new-selector")

        selected = await wait_for_first_visible(
            page,
            ["old-selector", "new-selector", "unused-selector"],
            timeout=250,
        )

        self.assertEqual("new-selector", selected)
        self.assertEqual(
            [
                ("old-selector", "visible", 250),
                ("new-selector", "visible", 250),
            ],
            page.attempts,
        )


class LoginPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_detects_a_scan_login_prompt(self):
        page = FakeLoginPage("扫码登录")

        self.assertTrue(await page_has_login_prompt(page))


class ClickFirstMatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_clicks_only_the_first_matching_element(self):
        page = FakeClickablePage()

        await click_first_match(page, "many-matches")

        self.assertTrue(page.result.clicked)

    async def test_detects_redirect_back_to_the_login_homepage(self):
        page = FakeLoginPage(None, url="https://creator.douyin.com/")

        self.assertTrue(await page_has_login_prompt(page))


class ConfirmMessageSentTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_input_clear_and_last_message_line_to_appear(self):
        page = FakeDeliveryPage()
        chat_input = FakeChatInput()

        await confirm_message_sent(
            page,
            chat_input,
            "first line\nunique verification line",
            timeout=1200,
        )

        self.assertEqual(
            [("(element) => !element.innerText.trim()", "chat-input-handle", 1200)],
            page.wait_function_calls,
        )
        self.assertEqual(("unique verification line", False), page.requested_text)
        self.assertEqual([("visible", 1200)], page.message_locator.wait_calls)


if __name__ == "__main__":
    unittest.main()
