"""configTool 登录流程的两条约束（纯逻辑，不依赖 tkinter / 浏览器）。

  · 抓取门禁只看 Cookie（`_has_login_cookie`），页面级信号不当门禁 ——
    check_login 的 DOM 兜底在登录过程中就成立，据此抓取会打断用户登录；
  · 自动流程（allow_reload=False）绝不调用 page.reload。

用假的 page/ctx 顶替 Playwright，只验证分支。
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
        """`logged_in` 但缺 user_id 不算数（与 check_login 对齐），否则会白触发抓取。"""
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
        """浅判定不许碰 page.content()（整页 DOM 序列化，探针 1.5 秒跑一次）。"""
        w, page = _worker((), ssr={})
        self.assertEqual(w._login_verdict()["state"], "UNKNOWN")
        self.assertEqual(page.content_calls, 0, "浅判定不该序列化 DOM")


class AutoFlowNeverReloadsTests(unittest.TestCase):
    """自动流程绝不刷新页面。"""

    def test_auto_grab_does_not_reload(self):
        w, page = _worker((), ssr={})
        w.read_account_info(grace=0.05, allow_reload=False)
        self.assertEqual(page.reloads, 0, "自动流程刷新了页面 —— 会打断用户的登录")

    def test_manual_grab_may_reload(self):
        """手动「立即抓取」是例外。只钉决策：把 _reload_for_profile 换成记录器。"""
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
