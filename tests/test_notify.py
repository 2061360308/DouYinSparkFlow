"""消息通知：规格 / Config 读写 / 校验 / 发送入口的单元测试。

不依赖网络：只测「配置解析 + 校验 + 分发」，真实推送由 core/notify.py 负责。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SCHEDULER_BACKEND", "noop")

from app.config import env_store, notify_spec
from app.config.models import Account, Config, validate
from core import notify


def _config_with(notifications):
    return Config(
        accounts=[Account(username="a", unique_id="1", cookies="[{}]", targets=["x"])],
        notifications=notifications,
    )


class NotifySpecTests(unittest.TestCase):
    def test_default_params_fills_known_fields(self):
        params = notify_spec.default_params("coolpush")
        self.assertTrue(params["coolpushqq"])
        self.assertFalse(params["coolpushwx"])
        self.assertEqual(params["coolpushskey"], "")

    def test_select_default(self):
        self.assertEqual(notify_spec.default_params("qmsg")["qmsg_type"], "private")

    def test_missing_required_reports_labels(self):
        missing = notify_spec.missing_required({"type": "bark", "bark_url": ""})
        self.assertIn("Bark URL", missing)

    def test_unknown_type_has_no_spec(self):
        self.assertEqual(notify_spec.spec_for("nope"), {})
        self.assertEqual(notify_spec.label_for("nope"), "nope")


class NotifyConfigTests(unittest.TestCase):
    def test_env_roundtrip(self):
        original = [
            {"type": "bark", "enabled": True, "bark_url": "https://api.day.app/k?x=1&y=2"},
            {"type": "coolpush", "enabled": False, "coolpushskey": "sk"},
        ]
        env = _config_with(original).to_env_map()
        self.assertIn("NOTIFY", env)
        back = Config.from_env_map(env).notifications
        self.assertEqual([n["type"] for n in back], ["bark", "coolpush"])
        self.assertEqual(back[0]["bark_url"], "https://api.day.app/k?x=1&y=2")
        self.assertFalse(back[1]["enabled"])

    def test_unknown_type_dropped(self):
        back = Config.from_env_map({"NOTIFY": '[{"type":"nope","x":1}]'}).notifications
        self.assertEqual(back, [])

    def test_validate_missing_required(self):
        issues = validate(_config_with([{"type": "bark", "enabled": True, "bark_url": ""}]))
        self.assertTrue(any("通知" in text for _level, text in issues))

    def test_validate_disabled_not_required(self):
        issues = validate(
            _config_with([{"type": "bark", "enabled": False, "bark_url": ""}])
        )
        self.assertFalse(any("通知" in text for _level, text in issues))

    def test_save_and_load_env_file(self):
        tmp = Path(tempfile.mkdtemp()) / ".env"
        env_store.save_config(_config_with([{"type": "bark", "bark_url": "https://a/b"}]), tmp)
        line = next(l for l in tmp.read_text(encoding="utf-8").splitlines() if l.startswith("NOTIFY="))
        self.assertIn("bark", line)
        loaded, _ = env_store.load_config(tmp)
        self.assertEqual(loaded.notifications[0]["bark_url"], "https://a/b")


class NotifyDispatchTests(unittest.TestCase):
    def test_unknown_type_returns_error(self):
        ok, message = notify.send_one({"type": "nope"}, "x")
        self.assertFalse(ok)
        self.assertIn("未知", message)

    def test_disabled_is_skipped(self):
        ok, message = notify.send_one({"type": "bark", "enabled": False}, "x")
        self.assertFalse(ok)
        self.assertIn("停用", message)

    def test_send_all_reports_each(self):
        results = notify.send_all([{"type": "nope"}, {"type": "bark", "enabled": False}], "x")
        self.assertEqual(len(results), 2)
        self.assertEqual([ok for _label, ok, _msg in results], [False, False])


if __name__ == "__main__":
    unittest.main()
