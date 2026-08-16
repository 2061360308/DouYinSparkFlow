import unittest

from core.tasks import click_first_match, page_has_login_prompt, wait_for_first_visible


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


if __name__ == "__main__":
    unittest.main()
