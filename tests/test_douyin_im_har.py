"""core.douyin_im 的真实报文抽样测试 —— 需要本机抓的 HAR，没有就整体 skip。

**这个文件依赖真实抓包，而 HAR 含登录凭据与好友资料，不入版本库。**
所以它默认在你自己的机器上才跑得起来；CI / 别人的 clone 会走 skipTest。

HAR 从哪来：跑 tools/record_har.py 录一段 `www.douyin.com/chat` 的流量。
然后把它放到 har_logs/ 下，或用环境变量指定：

    DOUYIN_IM_HAR=har_logs/你的.har python -m unittest tests.test_douyin_im_har

注意 HAR 的二进制响应体不在 content.text，而是同目录的 <sha1>.dat
（由 content._file 引用）—— 少了同目录的 .dat 文件，这里全部会失败。

那 20 项断言里有几条本质是「一次性快照」（比如「末轮 2043 的 has_more 为 False」
依赖录制时的会话总数），换一份 HAR 就可能不成立 —— 这是预期行为，不是 bug。
"""

import base64
import json
import os
import re
import unittest

from core.douyin_im import (
    decode_init_resp,
    decode_send_resp,
    decode_user_info,
    scrape_ssr,
)

HAR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "har_logs")


def _find_har():
    """按优先级找一个可用 HAR：环境变量 → har_logs/ 里最新的 .har。"""
    env = os.environ.get("DOUYIN_IM_HAR")
    if env:
        return env if os.path.isabs(env) else os.path.join(
            os.path.dirname(HAR_DIR), env)
    if not os.path.isdir(HAR_DIR):
        return None
    hars = [os.path.join(HAR_DIR, f) for f in os.listdir(HAR_DIR)
            if f.lower().endswith(".har")]
    if not hars:
        return None
    # 取最新的一份
    return max(hars, key=os.path.getmtime)


HAR = _find_har()


@unittest.skipUnless(HAR and os.path.exists(HAR),
                     "需要本机真实 HAR（不入库）：设置 DOUYIN_IM_HAR 或放到 har_logs/")
class HarSamplingTests(unittest.TestCase):
    entries = None

    @classmethod
    def setUpClass(cls):
        with open(HAR, "r", encoding="utf-8") as f:
            cls.entries = ((json.load(f).get("log") or {}).get("entries")) or []

    @classmethod
    def body_of(cls, e, which):
        """HAR 里二进制体落在同目录 .dat，用 content._file 引用 —— 必须回填。"""
        node = (e.get("request", {}).get("postData") or {}) if which == "req" \
            else (e.get("response", {}).get("content") or {})
        t = node.get("text")
        if t:
            if node.get("encoding") == "base64":
                return base64.b64decode(t)
            return t.encode("utf-8", errors="replace")
        fn = node.get("_file")
        if fn:
            fp = os.path.join(os.path.dirname(os.path.abspath(HAR)), fn)
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    return fh.read()
        return None

    def _bodies(self, entries, which="resp"):
        return [b for b in (self.body_of(e, which) for e in entries) if b]

    # ---- 2043 会话列表初始化 ----

    def test_finds_init_request(self):
        init = [e for e in self.entries
                if "get_message_by_init" in e.get("request", {}).get("url", "")]
        self.assertGreater(len(init), 0)

    def test_decodes_init_response_with_dat_backfill(self):
        init = [e for e in self.entries
                if "get_message_by_init" in e.get("request", {}).get("url", "")]
        self.assertGreater(len(self._bodies(init)), 0)

    def test_first_page_has_more_true(self):
        init = [e for e in self.entries
                if "get_message_by_init" in e.get("request", {}).get("url", "")]
        bodies = self._bodies(init)
        if not bodies:
            self.skipTest("没有可解的 2043 响应体")
        self.assertIs(decode_init_resp(bodies[0])["has_more"], True)

    def test_last_page_has_more_false(self):
        # ⚠️ 依赖录制时的会话总数：末轮必然 has_more=False（循环终止条件）
        init = [e for e in self.entries
                if "get_message_by_init" in e.get("request", {}).get("url", "")]
        bodies = self._bodies(init)
        if not bodies:
            self.skipTest("没有可解的 2043 响应体")
        self.assertIs(decode_init_resp(bodies[-1])["has_more"], False)

    # ---- 发送回执 ----

    def test_send_receipt_code_0_and_status_ok(self):
        sends = [e for e in self.entries
                 if "/v1/message/send" in e.get("request", {}).get("url", "")]
        bodies = self._bodies(sends)
        if not bodies:
            self.skipTest("这份 HAR 没走 HTTP 发送通道（可能走了 WebSocket）")
        r = decode_send_resp(bodies[-1])
        self.assertEqual(r["code"], 0)
        self.assertEqual(r["status"], "OK")

    def test_send_receipt_message_id_is_exact_big_int(self):
        sends = [e for e in self.entries
                 if "/v1/message/send" in e.get("request", {}).get("url", "")]
        bodies = self._bodies(sends)
        if not bodies:
            self.skipTest("这份 HAR 没走 HTTP 发送通道")
        mid = decode_send_resp(bodies[-1])["message_id"]
        self.assertTrue(mid and mid.isdigit())
        # JS 用 Number 读会丢成 ...000，Python 原生大整数不会
        self.assertFalse(mid.endswith("000"), f"疑似精度丢失: {mid}")

    # ---- 好友资料 ----

    def _peers(self):
        uis = [e for e in self.entries
               if "/aweme/v1/web/im/user/info/" in e.get("request", {}).get("url", "")]
        peers = []
        for e in uis:
            b = self.body_of(e, "resp")
            if b:
                peers.extend(decode_user_info(b))
        return uis, peers

    def test_resolves_peer_profiles(self):
        uis, peers = self._peers()
        self.assertGreater(len(peers), 0)

    def test_profiles_have_nickname_and_uid(self):
        _, peers = self._peers()
        if not peers:
            self.skipTest("没有资料可验")
        self.assertTrue(any(p["nickname"] for p in peers))
        self.assertTrue(any(p["uid"] for p in peers))

    def test_profiles_include_remark_holders(self):
        # remark_name 是存在性字段：只在你设过备注时整个 key 才出现
        _, peers = self._peers()
        if not peers:
            self.skipTest("没有资料可验")
        self.assertTrue(any(p["has_remark"] for p in peers))

    def test_sec_uid_unique_within_single_call(self):
        # 单次调用内 sec_uid 唯一 → 才能拿它当映射键
        uis, _ = self._peers()
        for e in uis:
            b = self.body_of(e, "resp")
            if not b:
                continue
            arr = [p["sec_uid"] for p in decode_user_info(b) if p["sec_uid"]]
            self.assertEqual(len(set(arr)), len(arr), "单次调用内 sec_uid 重复")

    def test_dedup_across_calls_yields_many_peers(self):
        # 跨调用会重复 → 监听器按 sec_uid 去重是必需的
        _, peers = self._peers()
        if not peers:
            self.skipTest("没有资料可验")
        secs = {p["sec_uid"] for p in peers if p["sec_uid"]}
        self.assertGreater(len(secs), 10)

    # ---- SSR 登录态 ----

    def test_finds_chat_document_response(self):
        docs = [e for e in self.entries
                if re.match(r"^https://www\.douyin\.com/(chat|$|\?)",
                            e.get("request", {}).get("url", ""))]
        self.assertGreater(len(docs), 0)

    def test_ssr_parses_without_choking_on_undefined(self):
        docs = [e for e in self.entries
                if re.match(r"^https://www\.douyin\.com/(chat|$|\?)",
                            e.get("request", {}).get("url", ""))]
        if not docs:
            self.skipTest("HAR 里没有 chat 页文档响应")
        b = self.body_of(docs[0], "resp")
        if not b:
            self.skipTest("chat 页响应体取不到")
        ss = scrape_ssr(b)
        # SSR 里有裸 $undefined，JSON.parse 会崩 —— 这里必须给出结论
        self.assertIn(ss["verdict"], ("logged_in", "logged_out"))

    def test_ssr_reports_logged_in(self):
        # ⚠️ 依赖录制时的登录态
        docs = [e for e in self.entries
                if re.match(r"^https://www\.douyin\.com/(chat|$|\?)",
                            e.get("request", {}).get("url", ""))]
        if not docs:
            self.skipTest("HAR 里没有 chat 页文档响应")
        b = self.body_of(docs[0], "resp")
        if not b:
            self.skipTest("chat 页响应体取不到")
        self.assertEqual(scrape_ssr(b)["verdict"], "logged_in")

    def test_ssr_yields_user_id_and_nickname(self):
        # 只断言「解出来了」，不打真实值 —— 这个输出可能被贴进仓库
        docs = [e for e in self.entries
                if re.match(r"^https://www\.douyin\.com/(chat|$|\?)",
                            e.get("request", {}).get("url", ""))]
        if not docs:
            self.skipTest("HAR 里没有 chat 页文档响应")
        b = self.body_of(docs[0], "resp")
        if not b:
            self.skipTest("chat 页响应体取不到")
        ss = scrape_ssr(b)
        self.assertTrue(ss["user_id"])
        self.assertTrue(ss["nickname"])


if __name__ == "__main__":
    unittest.main()
