import unittest

from core.web_chat import TargetNotFoundError, select_web_chat_target


class FakeTitle:
    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text


class FakeConversation:
    def __init__(self, title):
        self.title = title
        self.clicked = False

    def locator(self, selector):
        return FakeTitle(self.title)

    async def click(self):
        self.clicked = True


class FakeConversationList:
    def __init__(self, items):
        self.items = items

    async def all(self):
        return self.items


class FakeWebChatPage:
    def __init__(self, titles):
        self.items = [FakeConversation(title) for title in titles]
        self.waited_for = None

    async def wait_for_selector(self, selector, *, timeout):
        self.waited_for = (selector, timeout)

    def locator(self, selector):
        return FakeConversationList(self.items)


class WebChatSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_only_the_requested_web_chat_conversation(self):
        page = FakeWebChatPage(["another friend", " ʚ繁花ɞ🌸 "])

        selected = await select_web_chat_target(page, "ʚ繁花ɞ🌸", timeout=3200)

        self.assertEqual("ʚ繁花ɞ🌸", selected)
        self.assertFalse(page.items[0].clicked)
        self.assertTrue(page.items[1].clicked)

    async def test_prefers_exact_global_search_result(self):
        class SearchField:
            def __init__(self): self.first = self; self.value = None
            async def count(self): return 1
            async def fill(self, value): self.value = value
        class Result:
            def __init__(self): self.first = self; self.clicked = False
            async def count(self): return 1
            async def click(self): self.clicked = True
        class SearchPage:
            def __init__(self): self.search = SearchField(); self.result = Result(); self.recent_requested = False
            def locator(self, selector):
                if selector.startswith("input"): return self.search
                self.recent_requested = True
                return FakeConversationList([])
            def get_by_text(self, text, exact): return self.result
        page = SearchPage()
        selected = await select_web_chat_target(page, "ʚ繁花ɞ🌸")
        self.assertEqual("ʚ繁花ɞ🌸", selected)
        self.assertTrue(page.result.clicked)
        self.assertFalse(page.recent_requested)

    async def test_fails_instead_of_sending_to_the_wrong_conversation(self):
        page = FakeWebChatPage(["another friend"])

        with self.assertRaises(TargetNotFoundError):
            await select_web_chat_target(page, "ʚ繁花ɞ🌸")

        self.assertFalse(page.items[0].clicked)


if __name__ == "__main__":
    unittest.main()
