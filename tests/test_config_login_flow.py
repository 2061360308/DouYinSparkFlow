"""configTool 登录流程的两条硬约束（纯逻辑，不依赖 tkinter / 浏览器）。

钉住的是 2026-09-19 那次真实事故的根因 —— 当时「添加账号」在用户扫码后、
**还没提交短信验证码**的时候把页面刷新了，登录状态直接丢掉。两个原因：

  ① `check_login` 的页面级信号（SSR / DOM 兜底）被当成了「可以开始抓取」的
     门禁。它会在本地还没有可用登录态时说「已登录」，于是触发了抓取；
  ② 抓不到账号信息时 `read_account_info` 会刷新页面重试，而这一刷恰好
     打断用户正在进行的登录。

所以这里钉两条不变量：
  · 抓取门禁只看 Cookie（`_has_login_cookie`），页面级信号不当门禁；
  · 自动流程（allow_reload=False）**绝不**调用 page.reload。

另外钉住 `_login_verdict` 的两个细节：`logged_in` 必须带 user_id 才算数
（与 core.douyin_im.check_login 对齐），以及浅判定不许序列化整页 DOM
（探针 1.5 秒一次，落进 check_login 的 page.content() 会明显拖慢浏览器）。

用假的 page/ctx 顶替 Playwright —— 只验证分支，不开浏览器。
"""

import unittest

from configTool import browser_login as bl

SESSION = ("sessionid",)


class FakeCtx:
    """只实现被用到的那一个方法。"""

    def __init__(self, cookie_names=()):
        self._names = list(cookie_names)

    def cookies(self):
        return [{"name": n, "value": "x", "domain": ".douyin.com"} for n in self._names]


class FakePage:
    """记录 reload / content 的调用次数 —— 这两个是本次事故的关键指标。"""

    def __init__(self, cookie_names=()):
        self.reloads = 0
        self.content_calls = 0
        self.url = "https://www.douyin.com/chat"
        self.context = FakeCtx(cookie_names)

    def evaluate(self, *_args, **_kwargs):
        return None          # read_page_hook / localStorage 扫描都拿不到东西

    def on(self, *_args, **_kwargs):
        pass

    def remove_listener(self, *_args, **_kwargs):
        pass

    def reload(self, **_kwargs):
        self.reloads += 1

    def wait_for_timeout(self, _ms):
        pass

    def content(self):
        self.content_calls += 1
        return "<html></html>"


def _worker(cookie_names=(), ssr=None):
    page = FakePage(cookie_names)
    worker = bl.BrowserLoginWorker("unused-profile-dir")
    worker.ctx = page.context
    worker.page = page
    worker.mon = type("FakeMon", (), {"login": dict(ssr or {})})()
    return worker, page


class LoginVerdictTests(unittest.TestCase):
    """`_login_verdict` 的结论口径。"""

    def test_ssr_logged_in_needs_user_id(self):
        """★ `logged_in` 但缺 user_id 不算数 —— 与 check_login 的口径一致。

        少这一个条件，「页面认为已登录」就会被当成真的已登录，
        白白触发一次抓取。
        """
        w, _ = _worker(ssr={"verdict": "logged_in", "user_id": "10000000000000001"})
        self.assertEqual(w._login_verdict()["state"], "LOGGED_IN")

        w, _ = _worker(ssr={"verdict": "logged_in", "user_id": None})
        self.assertEqual(w._login_verdict()["state"], "UNKNOWN")

    def test_ssr_logged_out_with_session_cookie_is_expired(self):
        """有 sessionid 但 SSR 说未登录 == 服务端已把登录态作废 → EXPIRED。"""
        w, _ = _worker(SESSION, ssr={"verdict": "logged_out"})
        self.assertEqual(w._login_verdict()["state"], "EXPIRED")

    def test_ssr_logged_out_without_cookie_is_not_logged_in(self):
        w, _ = _worker((), ssr={"verdict": "logged_out"})
        self.assertEqual(w._login_verdict()["state"], "NOT_LOGGED_IN")

    def test_shallow_verdict_never_serializes_the_dom(self):
        """★ 浅判定不许碰 page.content()。

        探针 1.5 秒跑一次，而 page.content() 是整页 DOM 序列化 ——
        落进去会让用户正在操作的浏览器明显发顿。
        """
        w, page = _worker((), ssr={})
        self.assertEqual(w._login_verdict()["state"], "UNKNOWN")
        self.assertEqual(page.content_calls, 0, "浅判定不该序列化 DOM")


class AutoFlowNeverReloadsTests(unittest.TestCase):
    """★ 自动流程绝不刷新页面（本次事故的直接原因）。"""

    def test_auto_grab_does_not_reload(self):
        w, page = _worker((), ssr={})
        w.read_account_info(grace=0.05, allow_reload=False)
        self.assertEqual(page.reloads, 0, "自动流程刷新了页面 —— 会打断用户的登录")

    def test_manual_grab_may_reload(self):
        """手动「立即抓取」是例外：用户本人在浏览器前面，知道自己在干什么。

        这里只钉「决策」—— 把 _reload_for_profile 换成记录器，不去真跑它
        （真跑要等满 PROFILE_WAIT_SECONDS，测试会白等 15 秒）。
        """
        w, _ = _worker((), ssr={})
        calls = []
        w._reload_for_profile = lambda: calls.append(1)
        w.read_account_info(grace=0.05, allow_reload=True)
        self.assertEqual(len(calls), 1)

    def test_auto_grab_never_reaches_the_reload_branch(self):
        """自动流程连「要不要刷新」这一步都不该走到。"""
        w, _ = _worker((), ssr={})
        calls = []
        w._reload_for_profile = lambda: calls.append(1)
        w.read_account_info(grace=0.05, allow_reload=False)
        self.assertEqual(calls, [])


class CookieGateTests(unittest.TestCase):
    """抓取门禁只认 Cookie。"""

    def test_page_level_signal_is_not_a_gate(self):
        """页面说已登录、本地却没有 sessionid → 门禁必须仍然为假。"""
        w, _ = _worker((), ssr={"verdict": "logged_in", "user_id": "10000000000000001"})
        self.assertFalse(w._has_login_cookie())

    def test_session_cookie_opens_the_gate(self):
        w, _ = _worker(SESSION, ssr={"verdict": "logged_out"})
        self.assertTrue(w._has_login_cookie())


if __name__ == "__main__":
    unittest.main()
