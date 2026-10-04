"""app 网页界面：浏览器账号操作（AccountOperator）的单元测试。

用假 worker（不发真浏览器）验证会话状态机：
登录自动抓取判定、换账号保护、会话写入 profiles.json、
以及前端最后收到 saved 事件。同时覆盖 service 层把会话名单带进 get_config。
"""

from __future__ import annotations

import os
import queue
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SCHEDULER_BACKEND", "noop")
os.environ.setdefault("APP_SCHEDULE_AUTOREGISTER", "0")

from app.config import settings, profile_store
from app.browser.sessions import AccountOperator, BrowserSession
from app.web.bridge import Bridge
from app.web.service import Service


class FakeWorker:
    """假的 BrowserLoginWorker：命令记下来、事件放进队列由测试投喂。"""

    def __init__(self, profile_dir, fingerprint="", proxy=None):
        self.profile_dir = Path(profile_dir)
        self.fingerprint = fingerprint
        self.proxy = proxy
        self.events = queue.Queue()
        self.sent: list = []

    def start(self) -> None:
        pass

    def send(self, command, payload=None) -> None:
        self.sent.append((command, payload))

    def emit(self, kind, payload=None) -> None:
        self.events.put((kind, payload))


class AccountOperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # profiles.json 指到临时目录，不碰仓库里真实的对照表
        self._index_backup = profile_store.INDEX_FILE
        profile_store.INDEX_FILE = self.root / "profiles.json"
        # local.json 也指到临时目录（proxy_config 会读它）
        self._settings_backup = settings.SETTINGS_FILE
        settings.SETTINGS_FILE = self.root / "local.json"
        self.bridge = Bridge()
        self.factories: list = []

        def factory(**kwargs):
            worker = FakeWorker(profile_dir=kwargs["profile_dir"], fingerprint=kwargs.get("fingerprint", ""), proxy=kwargs.get("proxy"))
            self.factories.append(worker)
            return worker

        self.ops = AccountOperator(self.bridge, worker_factory=factory)

    def tearDown(self):
        profile_store.INDEX_FILE = self._index_backup
        settings.SETTINGS_FILE = self._settings_backup
        self.tmp.cleanup()

    def _drain(self) -> list:
        """一次性把桥里的事件全部取回，避免二次 drain 丢事件。"""
        return self.bridge.drain()

    def _collect(self) -> dict:
        """把桥里剩余事件按 kind 分组。"""
        events = self._drain()
        grouped: dict = {}
        for _, payload in events:
            kind = payload.get("kind")
            if kind in ("log", "status", "closed"):
                grouped.setdefault(kind, []).append(payload.get("data"))
            elif kind == "saved":
                grouped.setdefault("saved", []).append(payload.get("data"))
            elif kind == "grabbed":
                grouped.setdefault("grabbed", []).append(payload.get("data"))
        return grouped

    def _worker(self) -> FakeWorker:
        self.assertEqual(len(self.factories), 1)
        return self.factories[0]

    def test_login_start_creates_session_and_emits_logs(self):
        res = self.ops.login_start({"mode": "add"})
        self.assertTrue(res["ok"], res)
        session_id = res["session"]
        session = self.ops.sessions[session_id]
        self.assertEqual(session.mode, "login")
        self.assertEqual(session.profile_dir, self._worker().profile_dir)
        # worker 收到了 open 命令
        commands = [cmd for cmd, _ in self._worker().sent]
        self.assertIn("open", commands)
        # 桥上有 log（方式 / 配置目录 / 指纹）
        grouped = self._collect()
        logs = " | ".join(str(line) for line in grouped.get("log", []))
        self.assertIn("添加账号", logs)
        self.assertIn("配置目录", logs)

    def test_refresh_login_uses_existing_folder(self):
        profile_store.bind("abc123", "p123456789abc", nickname="盖瑞")
        res = self.ops.login_start({"mode": "refresh", "unique_id": "abc123"})
        session = self.ops.sessions[res["session"]]
        self.assertEqual(session.folder, "p123456789abc")

    def test_probe_logged_in_triggers_auto_grab(self):
        res = self.ops.login_start({"mode": "add"})
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        # 先 open（opened 事件触发探针启动条件）
        worker.events.put(("opened", None))
        self.ops.pump()
        worker.events.put(("probe", {"running": True, "logged_in": True, "login_state": "LOGGED_IN", "detected": {"unique_id": "abc123", "nickname": "盖瑞"}}))
        self.ops.pump()
        grabs = [p for cmd, p in worker.sent if cmd == "grab"]
        self.assertEqual(len(grabs), 1)
        # 添加账号不深判
        self.assertFalse(grabs[0]["deep_login"])

    def test_refresh_login_deep_login_true(self):
        profile_store.bind("abc123", "p123456789abc")
        res = self.ops.login_start({"mode": "refresh", "unique_id": "abc123"})
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        worker.events.put(("opened", None))
        self.ops.pump()
        worker.events.put(("probe", {"running": True, "logged_in": True, "login_state": "LOGGED_IN", "detected": {"unique_id": "abc123"}}))
        self.ops.pump()
        grabs = [p for cmd, p in worker.sent if cmd == "grab"]
        self.assertEqual(len(grabs), 1)
        self.assertTrue(grabs[0]["deep_login"])

    def test_refresh_switched_account_is_rejected(self):
        profile_store.bind("abc123", "p123456789abc", nickname="盖瑞")
        res = self.ops.login_start({"mode": "refresh", "unique_id": "abc123"})
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        worker.events.put(("grabbed", {
            "cookies": [{"name": "sessionid"}], "logged_in": True, "login_state": "LOGGED_IN",
            "detected": {"unique_id": "OTHER999", "nickname": "别人"},
        }))
        self.ops.pump()
        grouped = self._collect()
        self.assertEqual(grouped.get("saved", []), [])  # 换账号不保存
        logs = " | ".join(str(line) for line in grouped.get("log", []))
        self.assertIn("与当前账号", logs)

    def test_valid_grab_saves_account_and_emits_saved(self):
        res = self.ops.login_start({"mode": "add"})
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        worker.events.put(("grabbed", {
            "cookies": [{"name": "sessionid"}], "logged_in": True, "login_state": "LOGGED_IN",
            "detected": {"unique_id": "abc123", "nickname": "盖瑞"},
        }))
        self.ops.pump()
        grouped = self._collect()
        saved = grouped.get("saved", [])
        self.assertEqual(len(saved), 1)
        payload = saved[0]
        self.assertEqual(payload["kind"], "login")
        self.assertEqual(payload["mode"], "add")
        account = payload["account"]
        self.assertEqual(account["unique_id"], "abc123")
        self.assertEqual(account["username"], "盖瑞")
        self.assertIn("sessionid", account["cookies"])
        # grabbed 透传时剥掉大字段
        grabbed = grouped.get("grabbed", [])
        if grabbed:
            self.assertNotIn("storage_state", grabbed[0])
            self.assertNotIn("local_storage", grabbed[0])
        # profiles.json 已绑定目录
        index = profile_store.load()
        self.assertIn("abc123", index)
        self.assertEqual(index["abc123"]["folder"], session.folder)

    def test_bad_grab_retries_then_stops_polling(self):
        res = self.ops.login_start({"mode": "add"})
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        worker.events.put(("opened", None))
        self.ops.pump()
        # 三次「抓回来了但没识别到抖音号」→ 每次 probe 都会再触发一次 grab
        for _attempt in range(3):
            # probe 触发自动抓
            worker.events.put(("probe", {"running": True, "logged_in": True, "login_state": "LOGGED_IN", "detected": {"unique_id": "abc123"}}))
            self.ops.pump()
            grabs = [p for cmd, p in worker.sent if cmd == "grab"]
            self.assertEqual(len(grabs), _attempt + 1)
            # 抓回来不合格（没抖音号）→ 重试判定
            worker.events.put(("grabbed", {
                "cookies": [{"name": "sessionid"}], "logged_in": True, "login_state": "LOGGED_IN",
                "detected": {},
            }))
            self.ops.pump()
        grouped = self._collect()
        statuses = [str(s) for s in grouped.get("status", [])]
        self.assertTrue(any("已停止自动重试" in s for s in statuses))
        logs = " | ".join(str(line) for line in grouped.get("log", []))
        self.assertIn("改为手动", logs)
        # auto 已关 → 之后再 probe 也不再自动抓
        grabs_before = len([c for c, _ in worker.sent if c == "grab"])
        worker.events.put(("probe", {"running": True, "logged_in": True, "login_state": "LOGGED_IN", "detected": {"unique_id": "abc123"}}))
        self.ops.pump()
        grabs_after = len([c for c, _ in worker.sent if c == "grab"])
        self.assertEqual(grabs_after, grabs_before)

    def test_conversations_done_writes_names_and_saved(self):
        profile_store.bind("abc123", "p123456789abc", nickname="盖瑞")
        res = self.ops.conversations_start({"unique_id": "abc123"})
        self.assertTrue(res["ok"], res)
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        worker.events.put(("conversations", {"names": ["甲", "乙"], "stats": {"rounds": 3, "elapsed": 2.0, "hit_bottom": True}}))
        self.ops.pump()
        grouped = self._collect()
        saved = grouped.get("saved", [])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["kind"], "conversations")
        self.assertEqual(saved[0]["names"], ["甲", "乙"])
        index = profile_store.load()
        self.assertEqual(index["abc123"]["conversations"], ["甲", "乙"])
        self.assertTrue(session.done)

    def test_conversations_required_existing_folder(self):
        # 未绑定目录就拉会话 → 拒绝
        with self.assertRaises(ValueError):
            self.ops.conversations_start({"unique_id": "nobody"})

    def test_worker_done_closes_session(self):
        res = self.ops.login_start({"mode": "add"})
        session = self.ops.sessions[res["session"]]
        worker = self._worker()
        # worker 正常结束 → done → _close_session（幂等）
        worker.events.put(("done", None))
        self.ops.pump()
        grouped = self._collect()
        self.assertEqual(len(grouped.get("closed", [])), 1)
        self.assertNotIn(session.session_id, self.ops.sessions)

    def test_shutdown_sends_command_and_closes(self):
        res = self.ops.login_start({"mode": "add"})
        session = self.ops.sessions[res["session"]]
        result = self.ops.shutdown({"session": session.session_id})
        self.assertTrue(result["ok"])
        commands = [cmd for cmd, _ in self._worker().sent]
        self.assertIn("shutdown", commands)
        # worker 之后报 done → 会话关闭
        self._worker().events.put(("done", None))
        self.ops.pump()
        grouped = self._collect()
        self.assertEqual(len(grouped.get("closed", [])), 1)


class ServiceConversationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_file = self.root / ".env"
        self._index_backup = profile_store.INDEX_FILE
        profile_store.INDEX_FILE = self.root / "profiles.json"
        self._settings_backup = settings.SETTINGS_FILE
        settings.SETTINGS_FILE = self.root / "local.json"

    def tearDown(self):
        profile_store.INDEX_FILE = self._index_backup
        settings.SETTINGS_FILE = self._settings_backup
        self.tmp.cleanup()

    def test_save_then_get_config_attaches_conversations_and_folder(self):
        profile_store.bind("abc123", "p123456789abc", nickname="盖瑞")
        profile_store.set_conversations("abc123", ["甲", "乙"])
        # 复用同一个 Service 实例的读写路径
        service = Service(self.env_file)
        service.save_config({
            "config": {
                "run_time": "09:00:00", "tz": "Asia/Shanghai",
                "message_template": "x", "hitokoto_types": ["文学"],
                "accounts": [
                    {"username": "盖瑞", "unique_id": "abc123", "cookies": "[]", "targets": []}
                ],
            },
        })
        data = service.get_config()
        account = data["config"]["accounts"][0]
        self.assertEqual(account["conversations"], ["甲", "乙"])
        self.assertTrue(account["conversations_at"])
        self.assertEqual(account["profile_folder"], "p123456789abc")
        # 目录与指纹也带上了
        saved_again = Service(self.env_file).get_config()
        self.assertEqual(saved_again["config"]["accounts"][0]["conversations"], ["甲", "乙"])


if __name__ == "__main__":
    unittest.main()