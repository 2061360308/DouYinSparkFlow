"""core.douyin_im 的纯逻辑单测（不依赖浏览器 / 不依赖 HAR / 不含任何真实身份）。

这一段覆盖的是「判定与匹配」逻辑：滚动到底判据、步长守卫、归一化、匹配优先级、
conv_id 拆 uid、群聊判定、资料 join、信封返回码。

真实报文抽样在 tests/test_douyin_im_har.py（需要本机抓的 HAR，找不到就 skip）。
另有 LoggingTests 钉住日志分级契约（改造后 `verbose` 开关已被 logger 级别取代）。
身份数据一律由 core.douyin_im.fake_uid / fake_sec_uid 生成，
固定种子 → 结果可复现，但不含任何真值，可以安全提交。
"""
import inspect
import json
import unittest

from core import douyin_im
from core.douyin_im import (
    DouyinIM,
    ImMonitor,
    _envelope_code,
    _is_group,
    _norm,
    check_login,
    decode_user_info,
    envelope,
    fake_sec_uid,
    peer_uid_of,
    scan,
    split_message_lines,
)

# 固定假身份（由固定种子派生的常量，见 douyin_im._FAKE_*）
ME = "10000000000000001"
PEER_A = "10000000000000002"
PEER_B = "10000000000000003"


class _Stub(DouyinIM):
    """绕过 __init__ 的重活，只保留匹配所需的 _state。"""

    def __init__(self):
        self._state = {"user_id": ME}


class ScrollCriterionTests(unittest.TestCase):
    """两个量语义相反，不能混用：
    atBottom = 用户滚到底（scrollTop + clientHeight >= scrollHeight）
    notFilled = 内容还没填满容器（scrollHeight <= clientHeight）
    前端拿后者判「要不要补页」，不能拿来判「到底」。
    """

    @staticmethod
    def probe(scroll_top, client_h, scroll_h):
        return {
            "atBottom": scroll_top + client_h >= scroll_h - 1,
            "notFilled": scroll_h <= client_h + 1,
        }

    def test_long_list_top_is_not_bottom_and_not_filled_is_false(self):
        top = self.probe(0, 600, 10000)
        self.assertIs(top["atBottom"], False)
        # notFilled 恒 false —— 正说明它≠「到底」
        self.assertIs(top["notFilled"], False)

    def test_long_list_bottom_is_bottom_but_still_not_filled_false(self):
        bottom = self.probe(9400, 600, 10000)
        self.assertIs(bottom["atBottom"], True)
        self.assertIs(bottom["notFilled"], False)

    def test_short_list_is_filled_false_but_content_all_visible(self):
        short = self.probe(0, 600, 300)
        self.assertIs(short["notFilled"], True)
        self.assertIs(short["atBottom"], True)

    def test_step_never_exceeds_40_percent_of_window(self):
        for ch in (300, 600, 900):
            step = max(120, int(ch * 0.4))
            self.assertLessEqual(step, ch * 0.4 + 1, f"容器高 {ch}px")


class NormTests(unittest.TestCase):
    def test_strips_nbsp(self):
        self.assertEqual(_norm("甲\u00a0同\u00a0学"), _norm("甲同学"))

    def test_fullwidth_folds_to_ascii(self):
        self.assertEqual(_norm("ＡＢＣ"), _norm("abc"))

    def test_strips_zero_width(self):
        self.assertEqual(_norm("甲\u200b同学"), _norm("甲同学"))

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(_norm("  甲同学  "), _norm("甲同学"))

    def test_none_is_safe(self):
        self.assertEqual(_norm(None), "")

    def test_mixed_nbsp_and_space(self):
        # 前端 TextKeepSpaces 会把空格换成 \xa0，两种混着出现也要能归一
        self.assertEqual(_norm("甲\u00a0同 学"), _norm("甲同学"))

    def test_public_alias_matches_internal(self):
        from core.douyin_im import norm
        self.assertIs(norm, _norm)


class PeerUidTests(unittest.TestCase):
    def setUp(self):
        self.s = _Stub()

    def test_extracts_peer_not_self(self):
        lo, hi = sorted((PEER_A, ME), key=int)
        cid = f"0:1:{lo}:{hi}"
        self.assertEqual(self.s._peer_uid_of(cid), PEER_A)

    def test_same_conv_id_from_other_side_yields_me(self):
        lo, hi = sorted((PEER_A, ME), key=int)
        cid = f"0:1:{lo}:{hi}"
        self.assertEqual(peer_uid_of(cid, PEER_A), ME)

    def test_group_conv_id_returns_none(self):
        self.assertIsNone(self.s._peer_uid_of("7000000000000000001"))

    def test_self_to_self_yields_self(self):
        self.assertEqual(self.s._peer_uid_of(f"0:1:{ME}:{ME}"), ME)


class AssembleConvIdTests(unittest.TestCase):
    """复刻官方 assembleConvId：uid 按数值升序排列。"""

    @staticmethod
    def assemble(biz, a, b):
        return f"{biz}:1:{a}:{b}" if int(b) > int(a) else f"{biz}:1:{b}:{a}"

    def test_ascending_regardless_of_argument_order(self):
        lo, hi = sorted((PEER_A, ME), key=int)
        self.assertEqual(self.assemble(0, PEER_A, ME), f"0:1:{lo}:{hi}")
        self.assertEqual(self.assemble(0, ME, PEER_A), f"0:1:{lo}:{hi}")


class IsGroupTests(unittest.TestCase):
    def test_numeric_conv_id_is_group(self):
        self.assertIs(_is_group("7000000000000000001", None, None), True)

    def test_0_1_prefix_is_single(self):
        self.assertIs(_is_group(f"0:1:{ME}:111", None, None), False)

    def test_0_2_prefix_is_group(self):
        self.assertIs(_is_group("0:2:1:2", None, None), True)

    def test_participant_count_fallback_when_conv_id_has_no_signal(self):
        self.assertIs(_is_group("7:9:1:2", None, 5), True)

    def test_0_1_beats_participant_count(self):
        # `0:1:` 定义上就是 1:1，不该被 participant_count 翻案
        self.assertIs(_is_group("0:1:a:b", None, 5), False)

    def test_returns_none_when_undecidable(self):
        self.assertIsNone(_is_group(None, None, None))

    def test_conv_type_is_not_used_as_enum(self):
        # conv.type 实测是长整型会话 id，不是类型枚举
        lo, hi = sorted((PEER_A, ME), key=int)
        self.assertIs(_is_group(f"0:1:{lo}:{hi}", 7000000000000000002, None), False)


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.s = _Stub()
        self.sec = fake_sec_uid()
        self.item = {
            "remark": "甲同学", "nickname": "NickA", "douyin_id": "user_aaa",
            "uid": PEER_A, "sec_uid": self.sec,
            "title": "甲同学", "display": "甲同学",
        }

    def test_remark_has_highest_priority(self):
        _, how = self.s._match(self.item, {_norm("甲同学"): "甲同学"})
        self.assertEqual(how, "remark")

    def test_nickname_matched(self):
        _, how = self.s._match(self.item, {_norm("NickA"): "NickA"})
        self.assertEqual(how, "nickname")

    def test_douyin_id_matched(self):
        _, how = self.s._match(self.item, {_norm("user_aaa"): "user_aaa"})
        self.assertEqual(how, "douyin_id")

    def test_uid_matched(self):
        _, how = self.s._match(self.item, {_norm(PEER_A): "1"})
        self.assertEqual(how, "uid")

    def test_no_match_returns_none_pair(self):
        k, how = self.s._match(self.item, {"查无此人": "查无此人"})
        self.assertIsNone(k)
        self.assertIsNone(how)

    def test_remark_wins_over_nickname(self):
        both = {_norm("甲同学"): "甲同学", _norm("NickA"): "NickA"}
        _, how = self.s._match(self.item, both)
        self.assertEqual(how, "remark")

    def test_falls_back_to_dom_title_when_fiber_dead(self):
        dom_only = {"remark": None, "nickname": None, "douyin_id": "",
                    "uid": None, "sec_uid": None, "title": "甲同学", "display": "甲同学"}
        _, how = self.s._match(dom_only, {_norm("甲同学"): "甲同学"})
        self.assertEqual(how, "title")

    def test_dom_title_with_nbsp_still_matches(self):
        nb = "甲\u00a0同\u00a0学"
        dom = {"remark": None, "nickname": None, "douyin_id": "",
               "uid": "999", "sec_uid": "S9", "title": nb, "display": nb}
        k, how = self.s._match(dom, {_norm("甲同学"): "甲同学"})
        self.assertEqual(how, "title")
        self.assertIsNotNone(k)


class JoinTests(unittest.TestCase):
    class _MonStub:
        """ImMonitor 的最小替身（errors 是 _finish_scan 会读的字段）。"""

        def __init__(self, sec):
            self.errors = []
            self.peers = {
                sec: {"sec_uid": sec, "uid": PEER_A, "nickname": "NickA",
                      "remark": "甲同学", "unique_id": "user_aaa"},
            }

        def by_sec_uid(self):
            return {p["sec_uid"]: p for p in self.peers.values()}

        def by_uid(self):
            return {p["uid"]: p for p in self.peers.values()}

    def setUp(self):
        self.s = _Stub()
        self.sec = fake_sec_uid()
        self.s.mon = self._MonStub(self.sec)

    def test_joins_nickname_remark_douyin_id(self):
        it = {"sec_uid": self.sec, "uid": None, "nickname": None,
              "remark": None, "douyin_id": "", "title": "NickA", "display": "NickA"}
        self.s._join(it)
        self.assertEqual(it["nickname"], "NickA")
        self.assertEqual(it["remark"], "甲同学")
        self.assertEqual(it["douyin_id"], "user_aaa")

    def test_display_upgraded_to_remark(self):
        it = {"sec_uid": self.sec, "uid": None, "nickname": None,
              "remark": None, "douyin_id": "", "title": "NickA", "display": "NickA"}
        self.s._join(it)
        self.assertEqual(it["display"], "甲同学")

    def test_unresolved_keeps_none_and_falls_back_to_title(self):
        it = {"sec_uid": fake_sec_uid(body_len=8), "uid": None, "nickname": None,
              "remark": None, "douyin_id": "", "title": "某群", "display": "某群"}
        self.s._join(it)
        self.assertIsNone(it["nickname"])
        self.assertEqual(it["display"], "某群")


class _NoWaitPage:
    def wait_for_timeout(self, ms):
        pass


class _VListStub(_Stub):
    """模拟虚拟化会话列表：DOM 里恒只有「窗口内」那几条，滚动时窗口整体下移。

    尺寸刻意按真实量级搭（clientHeight 670 / 条目 100px / 步长守卫 0.4）
    → step = 268px ≈ 2.7 条 < 窗口 7 条，不会整屏跳过。
    若把尺寸改小（比如 total=7、clientHeight=3），step 会被抬到 120 的下限
    从而一步跳到底、中间的人永久漏掉 —— 那时测出来的"通过"是假的。
    """

    ITEM_H = 100
    CLIENT_H = 670

    def __init__(self, total=50):
        self._state = {"user_id": ME}
        self._scan_cache = {}
        self._walk_stat = {"stopped": "not-run", "steps": 0, "seen": {}}
        self._select_failed = []
        self.max_steps = 200
        self.timeout = 90.0
        self.settle_ms = 0
        self.total = total
        self.top = 0
        self.page = _NoWaitPage()
        # _finish_scan 的收尾日志会读 mon.errors；真身由 DouyinIM.__init__ 建立，
        # 这个桩绕过了 __init__，所以要自己补一个。资料一律对不上，
        # 让 _join 走「回退到 DOM 标题」那条分支（正是这里想覆盖的行为）。
        self.mon = JoinTests._MonStub("sec-that-matches-nothing")

    def _scroll_probe(self):
        sh = self.total * self.ITEM_H
        return {
            "found": True,
            "scrollTop": self.top,
            "clientHeight": self.CLIENT_H,
            "scrollHeight": sh,
            "atBottom": self.top + self.CLIENT_H >= sh - 1,
        }

    def _scroll_to(self, top):
        self.top = top

    def _read_window(self):
        first = self.top // self.ITEM_H
        n = self.CLIENT_H // self.ITEM_H + 1
        out = []
        for i in range(first, min(first + n, self.total)):
            out.append({
                "conv_id": f"0:1:{ME}:{2000 + i}", "title": f"U{i}",
                "nickname": None, "remark": None, "douyin_id": "",
                "uid": str(2000 + i), "sec_uid": None, "short_id": None,
                "is_group": False, "unread": 0, "is_muted": False,
                "is_current": False, "data_index": i, "dom_index": i - first,
                "from_fiber": True, "display": f"U{i}", "matched_by": None,
            })
        return out

    def _settle(self):
        self.settle_calls = getattr(self, "settle_calls", 0) + 1


class WalkTests(unittest.TestCase):
    """滚动骨架 _walk：去重、到底判定、提前中断不许谎报扫全。"""

    def setUp(self):
        self.s = _VListStub(total=50)

    def test_walks_all_and_dedupes_overlapping_windows(self):
        got = []
        for new_items in self.s._walk():
            got.extend(new_items)
        ids = [i["conv_id"] for i in got]
        self.assertEqual(len(ids), 50)          # 一条不多（重叠被吃掉）
        self.assertEqual(len(set(ids)), 50)     # 一条不少（没整屏跳过）

    def test_reaches_bottom(self):
        list(self.s._walk())
        self.s._finish_scan({}, [])
        self.assertEqual(self.s._walk_stat["stopped"], "reach-bottom")
        self.assertTrue(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["visited"], 50)

    def test_caller_break_is_not_reported_as_scanned_all(self):
        """外部提前 break 不能报 scanned_all —— 否则 missing 会被读成"确实不存在"。"""
        for i, _ in enumerate(self.s._walk()):
            if i >= 2:
                break
        self.s._finish_scan({}, [])
        self.assertEqual(self.s._walk_stat["stopped"], "caller-break")
        self.assertFalse(self.s.last_scan["scanned_all"])

    def test_no_container_reports_not_scanned(self):
        self.s._scroll_probe = lambda: {"found": False}
        self.assertEqual(list(self.s._walk()), [])
        self.s._finish_scan({}, [])
        self.assertEqual(self.s.last_scan["stopped"], "no-container")
        self.assertFalse(self.s.last_scan["scanned_all"])


class IterConversationsTests(unittest.TestCase):
    """任务二：枚举全部会话（旧 collect() 删掉后的替代 API）。"""

    def setUp(self):
        self.s = _VListStub(total=50)
        # 故意给一个对不上的 sec_uid —— 资料缺失时必须回退到 DOM 标题
        self.s.mon = JoinTests._MonStub("sec-that-matches-nothing")

    def test_yields_every_conversation_exactly_once(self):
        convs = list(self.s.iter_conversations())
        self.assertEqual(len(convs), 50)
        self.assertEqual(len({c["conv_id"] for c in convs}), 50)

    def test_items_are_joined_before_yield(self):
        convs = list(self.s.iter_conversations())
        self.assertTrue(all(c.get("display") for c in convs))
        self.assertEqual(convs[0]["display"], "U0")

    def test_last_scan_filled_after_enumeration(self):
        list(self.s.iter_conversations())
        self.assertIsNotNone(self.s.last_scan)
        self.assertTrue(self.s.last_scan["scanned_all"])
        self.assertEqual(self.s.last_scan["visited"], 50)
        self.assertEqual(self.s.last_scan["found"], {})
        self.assertEqual(self.s.last_scan["missing"], [])


class EnvelopeTests(unittest.TestCase):
    def test_reads_logout_code_8(self):
        buf = bytes([0x18, 0x08, 0x22, 0x02, 0x4F, 0x4B])
        self.assertEqual(_envelope_code(buf), 8)

    def test_reads_ok_code_0(self):
        buf = bytes([0x18, 0x00, 0x22, 0x02, 0x4F, 0x4B])
        self.assertEqual(_envelope_code(buf), 0)

    def test_empty_buffer_does_not_raise(self):
        self.assertIsNone(_envelope_code(b""))

    def test_scan_parses_varint_and_bytes_fields(self):
        fs = scan(bytes([0x18, 0x00, 0x22, 0x02, 0x4F, 0x4B]))
        self.assertEqual(len([f for f in fs if f[0] in (3, 4)]), 2)
        self.assertTrue(any(f[1] == 0 and f[2] == 0 for f in fs))
        self.assertTrue(any(f[1] == 2 and f[2] == b"OK" for f in fs))

    def test_scan_tolerates_truncated_and_garbage(self):
        self.assertIsInstance(scan(b"\x18"), list)
        self.assertIsInstance(scan(b"\xff\xff\xff\xff"), list)

    def test_envelope_reads_code_and_status(self):
        env = envelope(bytes([0x18, 0x00, 0x22, 0x02, 0x4F, 0x4B]))
        self.assertEqual(env["code"], 0)
        self.assertEqual(env["status"], "OK")


class DecodeUserInfoTests(unittest.TestCase):
    def setUp(self):
        self.sec_a = fake_sec_uid()
        self.sec_b = fake_sec_uid()
        self.raw = {
            "data": [
                {"uid": int(PEER_A), "sec_uid": self.sec_a, "nickname": "NickA",
                 "remark_name": "甲同学", "unique_id": "user_aaa"},
                {"uid": int(PEER_B), "sec_uid": self.sec_b, "nickname": "NickB",
                 "unique_id": "user_bbb"},
            ]
        }

    def test_decodes_two_entries(self):
        self.assertEqual(len(decode_user_info(self.raw)), 2)

    def test_uid_becomes_string_without_precision_loss(self):
        ui = decode_user_info(self.raw)
        self.assertEqual(ui[0]["uid"], PEER_A)

    def test_has_remark_true_when_present(self):
        ui = decode_user_info(self.raw)
        self.assertIs(ui[0]["has_remark"], True)

    def test_has_remark_false_when_absent(self):
        ui = decode_user_info(self.raw)
        self.assertIs(ui[1]["has_remark"], False)

    def test_missing_remark_name_yields_empty_string(self):
        ui = decode_user_info(self.raw)
        self.assertEqual(ui[1]["remark"], "")

    def test_non_dict_returns_empty(self):
        self.assertEqual(decode_user_info("nonsense"), [])

    def test_bad_json_bytes_return_empty(self):
        self.assertEqual(decode_user_info(b"{not json"), [])


class SplitMessageLinesTests(unittest.TestCase):
    """消息正文断行口径（`split_message_lines`）。

    链路上有两种 `\\n`，必须都认：
      - **字面** `\\`+`n`：`.env` 的 MESSAGE_TEMPLATE，dotenv 读出来不做 unescape
      - **真**换行 `U+000A`：一言正文、手写 .env 的引号多行值

    历史 bug：只认字面 `\\n` → 真换行被 `keyboard.type` 静默吞掉，三行挤成一行。
    反过来只认真换行 → `.env` 的默认模板整段变成一行。所以两个方向都要钉。
    """

    def test_literal_backslash_n_splits(self):
        """正常路径：.env 出来的字面 \\n 必须断行。"""
        self.assertEqual(split_message_lines("甲\\n乙"), ["甲", "乙"])

    def test_real_newline_splits(self):
        """第三方内容带真换行 —— 不能吞。"""
        self.assertEqual(split_message_lines("甲\n乙"), ["甲", "乙"])

    def test_literal_crlf_splits_without_leaking_cr(self):
        """字面 \\r\\n：收成一段分隔，**绝不能漏下一个裸露的 \\r**。

        注意这里用的是 Python 源码里的 `"\\r\\n"` —— 它是 **4 个字符**
        （反斜杠 r 反斜杠 n），正是 `dotenv` 从 .env 读出来的形态。
        别和真 CRLF 混了：那才 2 字符。
        """
        got = split_message_lines("甲\\r\\n乙")
        self.assertEqual(got, ["甲", "乙"])
        self.assertNotIn("\r", "".join(got), "漏下了裸露的 \\r，会打进草稿")

    def test_real_crlf_splits(self):
        self.assertEqual(split_message_lines("甲\r\n乙"), ["甲", "乙"])

    def test_default_template_shape(self):
        """回归：默认模板必须断成 3 段（[盖瑞] / —— / [API]）。"""
        src = "[盖瑞]今日火花[加一]\\n—— 右边 每日一言 左边 ——\\n[API]"
        self.assertEqual(len(split_message_lines(src)), 3)

    def test_no_newline_is_single_segment(self):
        self.assertEqual(split_message_lines("单行"), ["单行"])

    def test_trailing_literal_n_keeps_empty_tail(self):
        """尾部空段不会多键入内容 —— 调用方的 `if line:` 负责挡掉。

        但段数仍是 2，意味着段间会按一次 Shift+Enter。这是既有的收尾行为，
        测试把它钉住，免得日后被当成 bug"修"掉。
        """
        self.assertEqual(split_message_lines("甲\\n"), ["甲", ""])

    def test_empty_and_none_are_safe(self):
        self.assertEqual(split_message_lines(""), [""])
        self.assertEqual(split_message_lines(None), ["None"])  # str(None)，不炸

    def test_normalization_invariant(self):
        """不变量：断完之后不该再残留【字面 \\n】或【真 \\r】。

        这才是这个函数的**规格**——段数只是实现细节。
        """
        samples = ["甲\\n乙", "甲\n乙", "甲\\n\n乙", "甲\\r\\n乙", "甲\r\n乙",
                   "甲\\r\n乙", "甲\n\\n乙", "甲\\r\\r\\n乙", "单行"]
        for src in samples:
            with self.subTest(src=src):
                joined = "\n".join(split_message_lines(src))
                self.assertNotIn("\\n", joined, f"{src!r} 残留字面 \\n")
                self.assertNotIn("\r", joined, f"{src!r} 残留真 \\r")

    def test_mixed_forms_both_split(self):
        """混用（字面 + 真）两次都要断：段内容正确，段数=3。"""
        self.assertEqual(split_message_lines("甲\\n\n乙"), ["甲", "", "乙"])


class _FakePage:
    """只实现 ImMonitor 用到的两个方法：on() 和（此处不需要的）其余一概不管。"""

    def __init__(self):
        self.listeners = {}

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        self.listeners.get(event, []).remove(cb)


class LoggingTests(unittest.TestCase):
    """沉默期改造后的日志契约。

    这次改造把 `verbose` 开关换成了真正的 logger 分级，所以要有测试钉住：
    【1】模块级 logger 存在且名字固定；
    【2】`verbose` / `log_fn` / `log()` / `ImMonitor._log` 已彻底不存在
         （否则就是改造没做干净，旧开关会和新 logger 双重过滤）；
    【3】关键事件按预期级别落日志 —— 用 assertLogs 真正捕获，
         而不是"看着代码觉得它该打 info"。
    """

    def test_module_logger_name_is_stable(self):
        self.assertEqual(douyin_im.logger.name, "douyin_im")

    def test_removed_log_plumbing(self):
        """旧的四个日志开关/通道必须全没了。"""
        self.assertFalse(hasattr(ImMonitor, "_log"))
        self.assertFalse(hasattr(DouyinIM, "log"))
        src = inspect.signature(ImMonitor.__init__)
        self.assertNotIn("verbose", src.parameters)
        self.assertNotIn("verbose", inspect.signature(DouyinIM.__init__).parameters)
        self.assertNotIn("log", inspect.signature(check_login).parameters)

    def test_im_monitor_login_logs_at_info(self):
        """收到登录 SSR → [LOGIN] 走 info。"""
        mon = ImMonitor(_FakePage())
        body = json.dumps({
            "odin": {"user_id": ME},
            "user": {"isLogin": True, "nickname": "NickA"},
        }).encode("utf-8")
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            mon._handle_login(body)
        self.assertTrue(any(r.levelname == "INFO" and "[LOGIN]" in r.getMessage()
                            for r in cm.records), cm.output)

    def test_im_monitor_init_round_logs_at_debug(self):
        """列表页轮次属于机械进度 → 必须是 debug，不然 Info 级会刷屏。"""
        mon = ImMonitor(_FakePage())
        with self.assertLogs("douyin_im", level="DEBUG") as cm:
            mon._handle_init(b"")
        self.assertTrue(any(r.levelname == "DEBUG" for r in cm.records), cm.output)

    def test_send_success_is_info_failure_is_warning(self):
        """发送回执按成败分流：成了 info，没成 warning。"""
        mon = ImMonitor(_FakePage())
        mon.sends = []

        def _fake_decode(ok):
            return lambda body: {"ok": ok, "code": 0 if ok else 8,
                                 "status": "OK" if ok else "ERR", "message_id": None}

        real = douyin_im.decode_send_resp
        try:
            douyin_im.decode_send_resp = _fake_decode(True)
            with self.assertLogs("douyin_im", level="DEBUG") as cm:
                mon._handle_send(b"")
            self.assertTrue(any(r.levelname == "INFO" for r in cm.records), cm.output)

            douyin_im.decode_send_resp = _fake_decode(False)
            with self.assertLogs("douyin_im", level="DEBUG") as cm:
                mon._handle_send(b"")
            self.assertTrue(any(r.levelname == "WARNING" for r in cm.records),
                            cm.output)
        finally:
            douyin_im.decode_send_resp = real

    def test_check_login_does_not_take_log_param(self):
        """结论行改由调用方记录 —— check_login 自己不再打日志。"""
        params = inspect.signature(check_login).parameters
        self.assertNotIn("log", params)


if __name__ == "__main__":
    unittest.main()
