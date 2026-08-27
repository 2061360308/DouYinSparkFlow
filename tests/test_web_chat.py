import unittest

from core.web_chat import (
    TargetNotFoundError,
    UserInfoCollector,
    list_visible_web_chat_targets,
    select_web_chat_target,
)


class FakeTitle:
    def __init__(self, text):
        self.text = text

    async def inner_text(self):
        return self.text


class FakeConversation:
    def __init__(self, title, visible=True):
        self.title = title
        self.visible = visible
        self.clicked = False

    def locator(self, selector):
        return FakeTitle(self.title)

    async def click(self):
        self.clicked = True

    async def is_visible(self):
        return self.visible


class FakeConversationList:
    def __init__(self, items):
        self.items = items

    async def all(self):
        return self.items


class FakeWebChatPage:
    def __init__(self, titles, visible=None):
        visible = visible or [True] * len(titles)
        self.items = [
            FakeConversation(title, is_visible)
            for title, is_visible in zip(titles, visible)
        ]
        self.waited_for = None

    async def wait_for_selector(self, selector, *, timeout):
        self.waited_for = (selector, timeout)

    def locator(self, selector):
        return FakeConversationList(self.items)


class WebChatSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_info_collector_maps_stable_id_to_current_aliases(self):
        class Response:
            url = "https://www.douyin.com/aweme/v1/web/im/user/info/?aid=6383"
            status = 200

            async def json(self):
                return {
                    "data": [
                        {
                            "sec_uid": "stable-user-id",
                            "short_id": "123456",
                            "unique_id": "old-search-id",
                            "nickname": "新的昵称",
                            "remark_name": "我的备注",
                        }
                    ]
                }

        collector = UserInfoCollector()
        await collector.handle_response(Response())

        identity = collector.get("stable-user-id")
        self.assertEqual("stable-user-id", identity.sec_uid)
        self.assertEqual(
            ("我的备注", "新的昵称", "old-search-id", "123456"),
            identity.aliases,
        )

    async def test_selects_renamed_friend_using_current_identity_alias(self):
        page = FakeWebChatPage(["新的昵称", "another friend"])

        selected = await select_web_chat_target(
            page,
            "旧的昵称",
            aliases=("我的备注", "新的昵称", "old-search-id"),
        )

        self.assertEqual("新的昵称", selected)
        self.assertTrue(page.items[0].clicked)
        self.assertFalse(page.items[1].clicked)

    async def test_lists_visible_unique_conversations_for_task_picker(self):
        page = FakeWebChatPage(
            [" wzlovegsy ", "gsy", "wzlovegsy", "隐藏"],
            visible=[True, True, True, False],
        )

        targets = await list_visible_web_chat_targets(page, timeout=3200)

        self.assertEqual(["wzlovegsy", "gsy"], targets)
        self.assertEqual((".conversationConversationItemwrapper", 3200), page.waited_for)

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
        self.assertTrue(page.recent_requested)

    async def test_search_result_clicks_send_message_button(self):
        class SearchField:
            def __init__(self):
                self.first = self

            async def count(self):
                return 1

            async def fill(self, _value):
                return None

        class SendButton:
            def __init__(self):
                self.clicked = False

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self):
                self.clicked = True

        class Result(FakeConversation):
            def __init__(self):
                super().__init__("wzlovegsy")
                self.send_button = SendButton()

            def locator(self, selector):
                if "发消息" in selector:
                    return self.send_button
                return super().locator(selector)

        class Results:
            def __init__(self, item):
                self.items = [item]
                self.first = item

            async def count(self):
                return 1

            async def all(self):
                return self.items

        class Page:
            def __init__(self):
                self.search = SearchField()
                self.result = Result()

            def locator(self, selector):
                if selector == ".conversationConversationItemwrapper":
                    return FakeConversationList([])
                return self.search

            def get_by_text(self, _text, exact):
                return Results(self.result)

        page = Page()

        selected = await select_web_chat_target(page, "wzlovegsy")

        self.assertEqual("wzlovegsy", selected)
        self.assertTrue(page.result.send_button.clicked)
        self.assertFalse(page.result.clicked)

    async def test_ignores_hidden_duplicate_and_clicks_visible_conversation(self):
        page = FakeWebChatPage(
            ["ʚ繁花ɞ🌸", "ʚ繁花ɞ🌸"],
            visible=[False, True],
        )

        selected = await select_web_chat_target(page, "ʚ繁花ɞ🌸")

        self.assertEqual("ʚ繁花ɞ🌸", selected)
        self.assertFalse(page.items[0].clicked)
        self.assertTrue(page.items[1].clicked)

    async def test_global_search_ignores_hidden_exact_result(self):
        class SearchField:
            def __init__(self): self.first = self
            async def count(self): return 1
            async def fill(self, _value): return None
        class Results:
            def __init__(self):
                self.items = [FakeConversation("wzlovegsy", False), FakeConversation("wzlovegsy", True)]
                self.first = self.items[0]
            async def count(self): return len(self.items)
            async def all(self): return self.items
        class SearchPage:
            def __init__(self): self.search = SearchField(); self.results = Results()
            def locator(self, selector):
                if selector == ".conversationConversationItemwrapper":
                    return FakeConversationList([])
                return self.search
            def get_by_text(self, _text, exact): return self.results
        page = SearchPage()

        selected = await select_web_chat_target(page, "wzlovegsy")

        self.assertEqual("wzlovegsy", selected)
        self.assertFalse(page.results.items[0].clicked)
        self.assertTrue(page.results.items[1].clicked)

    async def test_clicks_already_visible_conversation_before_opening_search(self):
        class Page:
            def __init__(self):
                self.item = FakeConversation("wzlovegsy", True)
                self.search_requested = False

            def locator(self, selector):
                if selector == ".conversationConversationItemwrapper":
                    return FakeConversationList([self.item])
                self.search_requested = True
                raise AssertionError("search should not open when the target is already visible")
        page = Page()

        selected = await select_web_chat_target(page, "wzlovegsy")

        self.assertEqual("wzlovegsy", selected)
        self.assertTrue(page.item.clicked)
        self.assertFalse(page.search_requested)

    async def test_ignores_same_name_text_outside_conversation_list(self):
        class Results:
            def __init__(self, item):
                self.items = [item]
                self.first = item

            async def count(self):
                return len(self.items)

            async def all(self):
                return self.items

        class Page(FakeWebChatPage):
            def __init__(self):
                super().__init__(["wzlovegsy"])
                self.decoy = FakeConversation("wzlovegsy")

            def get_by_text(self, _text, exact):
                return Results(self.decoy)

        page = Page()

        selected = await select_web_chat_target(page, "wzlovegsy")

        self.assertEqual("wzlovegsy", selected)
        self.assertTrue(page.items[0].clicked)
        self.assertFalse(page.decoy.clicked)

    async def test_fails_instead_of_sending_to_the_wrong_conversation(self):
        page = FakeWebChatPage(["another friend"])

        with self.assertRaises(TargetNotFoundError):
            await select_web_chat_target(page, "ʚ繁花ɞ🌸")

        self.assertFalse(page.items[0].clicked)


if __name__ == "__main__":
    unittest.main()
