"""configTool 配置数据层的纯逻辑单测（不依赖 tkinter）。

只测 `models.Config` 的序列化往返 —— 尤其是 MESSAGE_TEMPLATE 的换行编码，
这是 GUI（真换行）与 `.env`（字面 `\\n`）之间的桥，两侧必须严格对称。

⚠️ configTool 已经是包，直接 `from configTool import models` 即可
   （从仓库根跑测试时仓库根就在 sys.path 上）。models.py 是纯数据层，
   不 import tkinter，可以安全在无 GUI 环境跑。
"""
import unittest
from pathlib import Path

from configTool import models


def _roundtrip(gui_text: str) -> tuple:
    """GUI 文本 → to_env_map（落盘形态）→ from_env_map（读回形态）。"""
    cfg = models.Config.from_env_map({})
    cfg.message_template = gui_text
    disk = cfg.to_env_map()["MESSAGE_TEMPLATE"]
    back = models.Config.from_env_map({"MESSAGE_TEMPLATE": disk}).message_template
    return disk, back


class MessageTemplateRoundTripTests(unittest.TestCase):
    """GUI ↔ .env 的换行编码必须往返对称。"""

    def test_real_newlines_become_literal(self):
        """GUI 里的真换行 → 磁盘上是字面 \\n（单行）。"""
        disk, back = _roundtrip("甲\n乙")
        self.assertEqual(disk, "甲\\n乙")
        self.assertEqual(back, "甲\n乙")

    def test_three_lines_roundtrip(self):
        gui = "[盖瑞]今日火花[加一]\n—— 每日一句 ——\n[API]"
        disk, back = _roundtrip(gui)
        self.assertEqual(disk, "[盖瑞]今日火花[加一]\\n—— 每日一句 ——\\n[API]")
        self.assertEqual(back, gui)

    def test_real_crlf_is_normalized_not_doubled(self):
        """★ 真 CRLF 必须收成**一个**字面 \\n，不能变成两个（空一行）。

        这是唯一真正依赖 replace 顺序的地方：
          正确 = replace("\\r\\n","\\n") 再 replace("\\r","\\n")
          写反 = 先 replace("\\r","\\n")，CRLF 里的 \\r 被单独吃掉 → "\\n\\n"
        若顺序写反，GUI 里一个回车会变成空行。
        """
        disk, back = _roundtrip("甲\r\n乙")
        self.assertEqual(disk, "甲\\n乙", "真 CRLF 被拆成了两个字面 \\n（多空一行）")
        self.assertEqual(back, "甲\n乙")

    def test_bare_cr_is_normalized(self):
        """孤立 \\r 也要收成换行。"""
        disk, _ = _roundtrip("甲\r乙")
        self.assertEqual(disk, "甲\\n乙")

    def test_disk_literal_crlf_is_read_back_without_leaking_cr(self):
        """磁盘上是字面 \\r\\n（老 .env 的写法）→ 读回真换行，不留裸露 \\r。"""
        back = models.Config.from_env_map(
            {"MESSAGE_TEMPLATE": "甲\\r\\n乙"}
        ).message_template
        self.assertEqual(back, "甲\n乙")
        self.assertNotIn("\r", back, "读回后残留了裸露的 \\r")

    def test_empty_and_single_line(self):
        for gui in ("", "单行"):
            with self.subTest(gui=gui):
                disk, back = _roundtrip(gui)
                self.assertEqual(back, gui)

    def test_to_env_map_is_idempotent(self):
        """跑两遍 to_env_map 结果必须一致（GUI 反复保存不会漂移）。"""
        cfg = models.Config.from_env_map({})
        cfg.message_template = "甲\n乙\r\n丙"
        first = cfg.to_env_map()["MESSAGE_TEMPLATE"]
        again = models.Config.from_env_map(
            cfg.to_env_map()
        ).to_env_map()["MESSAGE_TEMPLATE"]
        self.assertEqual(first, again)


class EnvKeysTests(unittest.TestCase):
    """BASE_ENV_KEYS 与 dataclass / to_env_map / from_env_map 四处必须齐。"""

    def test_no_key_is_silently_dropped(self):
        m = models.Config.from_env_map({}).to_env_map()
        missing = [k for k in models.BASE_ENV_KEYS if k not in m]
        extra = [k for k in m if k not in models.BASE_ENV_KEYS]
        self.assertEqual(missing, [], f"这些键在 to_env_map 里丢了: {missing}")
        self.assertEqual(extra, [], f"这些键没登记进 BASE_ENV_KEYS: {extra}")

    def test_env_map_is_flat_strings(self):
        """落盘的每个值都必须是 str（dotenv 只认字符串）。"""
        for k, v in models.Config.from_env_map({}).to_env_map().items():
            with self.subTest(key=k):
                self.assertIsInstance(v, str, f"{k} 不是字符串")


class DefaultsAlignmentTests(unittest.TestCase):
    """★ 四处默认值必须一致（2026-09-19 对齐过一轮）。

    同一个键有四个"默认"来源，历史上已经漂移过一次：
        configTool/models.py  DEFAULT_*        —— GUI 新建配置的初始值
        .env.example                            —— 给人抄的示例值
        docs/static/js/main.js  form            —— 网页版初始值
        utils/config.py  os.getenv(..., 兜底)   —— 程序无 .env 时的底裤

    漂移不会报错，只会让"干净克隆"和"GUI 生成"两条路径行为不同 —— 极难排查。
    这个测试把四处钉在一起。

    ⚠️ docs/main.js 是手抄的、没有构建期联动，是最容易漏的一处。
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls):
        import re

        cls.re = re
        # ① configTool DEFAULT_*
        cls.ct = {
            k[len("DEFAULT_"):]: v
            for k, v in vars(models).items()
            if k.startswith("DEFAULT_") and not k.startswith("DEFAULT_HITOKOTO")
        }
        # ② .env.example
        cls.env = dict(
            re.findall(
                r"^([A-Z_]+)=(.*)$",
                (cls.ROOT / ".env.example").read_text(encoding="utf-8"),
                re.M,
            )
        )
        # ③ docs/static/js/main.js 的 form 初始值
        js = (cls.ROOT / "docs/static/js/main.js").read_text(encoding="utf-8")
        block = js.split("const form = reactive({", 1)[1].split("ACCOUNTS:", 1)[0]
        cls.js = dict(re.findall(r'^\s{6}([A-Z_]+):\s*"?(.*?)"?,?\s*$', block, re.M))
        # ④ utils/config.py 的 os.getenv 兜底
        cls.pyf = dict(
            re.findall(
                r'os\.getenv\("([A-Z_]+)",\s*"([^"]*)"\)',
                (cls.ROOT / "utils/config.py").read_text(encoding="utf-8"),
            )
        )

    def _check(self, env_key, default_attr):
        ct = self.ct.get(default_attr)
        env = self.env.get(env_key)
        js = self.js.get(env_key)
        pyf = self.pyf.get(env_key)
        four = {"configTool": str(ct), ".env.example": str(env),
                "docs": str(js), "config.py": str(pyf)}
        self.assertEqual(
            len(set(four.values())), 1,
            f"{env_key} 四处默认值不一致: {four}",
        )

    def test_ready_timeout_aligned(self):
        self._check("IM_READY_TIMEOUT", "IM_READY_TIMEOUT")

    def test_friend_list_wait_aligned(self):
        self._check("FRIEND_LIST_WAIT_TIME", "FRIEND_LIST_WAIT_TIME")

    def test_log_level_aligned(self):
        self._check("LOG_LEVEL", "LOG_LEVEL")

    def test_all_numeric_defaults_aligned(self):
        for env_key, attr in [
            ("BROWSER_ACTION_TIMEOUT", "BROWSER_ACTION_TIMEOUT"),
            ("IM_SCAN_TIMEOUT", "IM_SCAN_TIMEOUT"),
            ("IM_MAX_STEPS", "IM_MAX_STEPS"),
            ("TASK_RETRY_TIMES", "TASK_RETRY_TIMES"),
        ]:
            with self.subTest(key=env_key):
                self._check(env_key, attr)

    def test_log_level_matches_dropdown_option(self):
        """★ 默认日志级别必须**逐字**等于下拉框里的选项。

        tkinter Combobox 对不在 values 里的值不会高亮匹配项 → 框看着是空的，
        用户会以为没设置。`utils.logger` 内部 upper/lower 了，所以两种写法
        日志行为**相同**，踩坑时不会报错、只会在 GUI 上显示异常。
        """
        self.assertIn(
            models.DEFAULT_LOG_LEVEL, models.LOG_LEVEL_OPTIONS,
            f"{models.DEFAULT_LOG_LEVEL!r} 不在下拉选项 {models.LOG_LEVEL_OPTIONS} 里",
        )

    def test_docs_log_level_matches_dropdown_option(self):
        """网页版同上：main.js 的默认值必须在其 log_level_options 里。"""
        js = (self.ROOT / "docs/static/js/main.js").read_text(encoding="utf-8")
        options = self.re.findall(r'value:\s*"(\w+)"', js)
        self.assertIn(self.js["LOG_LEVEL"], options,
                      f"{self.js['LOG_LEVEL']!r} 不在网页版选项 {options} 里")


if __name__ == "__main__":
    unittest.main()
