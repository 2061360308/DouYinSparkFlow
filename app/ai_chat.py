"""陪聊生命周期和 API 测试；所有耗时工作在后台执行。"""
from __future__ import annotations

import copy
import json
import sys
import threading
from collections import deque
from contextlib import suppress
from datetime import datetime

from core.ai.config import AIConfig
from core.ai.providers import create_provider, ProviderError


class ChatController:
    def __init__(self, callback=None, runner=None):
        self.callback = callback
        self.runner = runner
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads = []
        self._logs = deque(maxlen=100)
        self._sent = 0
        self._errors = 0
        self._test_busy = False
        self._test_result = None
        self._test_thread = None

    def _emit(self, kind, message):
        with self._lock:
            if kind == "sent":
                self._sent += 1
            if kind == "error":
                self._errors += 1
            self._logs.append({"time": datetime.now().strftime("%H:%M:%S"),
                               "kind": kind, "message": message})
        if self.callback:
            self.callback(kind, message)
        # Metadata only: retain diagnostic stages without recording chat text.
        print(f"[AI] {kind}: {message}", file=sys.stderr, flush=True)

    def status(self, _payload=None):
        with self._lock:
            running = any(t.is_alive() for t in self._threads)
            return {"running": running, "stopping": running and self._stop.is_set(),
                    "sent": self._sent, "errors": self._errors, "logs": list(self._logs),
                    "test_busy": self._test_busy, "test_result": self._test_result}

    def start(self, config):
        with self._lock:
            if any(t.is_alive() for t in self._threads):
                raise ValueError("陪聊已经在运行或正在停止")
            snapshot = copy.deepcopy(config)
            snapshot.ai_chat.selected()
            # 工厂/协议也预检，API Key 只在后端持有。
            provider = create_provider(snapshot.ai_chat)
            provider.close()
            accounts = [a for a in snapshot.accounts if a.ai_targets]
            if not accounts:
                raise ValueError("请先在 AI 陪聊页面选择至少一个好友或群聊")
            for account in accounts:
                cookies = json.loads(account.cookies or "[]")
                if not isinstance(cookies, list) or not cookies:
                    raise ValueError(f"{account.username} 没有可用的 Cookies，请刷新登录信息")
            self._stop = threading.Event()
            self._sent = self._errors = 0
            self._logs.clear()
            self._threads = []
            for account in accounts:
                thread = threading.Thread(target=self._run, args=(account, snapshot), daemon=True,
                                          name=f"ai-chat-{account.unique_id}")
                self._threads.append(thread)
                thread.start()
        return self.status()

    def _run(self, account, config):
        try:
            if self.runner is None:
                from core.ai.runner import run_account
                runner = run_account
            else:
                runner = self.runner
            runner(account, config.ai_chat, config, self._stop, self._emit)
        except ValueError as exc:
            self._emit("error", str(exc))
        except Exception:
            self._emit("error", f"{account.username}：陪聊连接异常，请检查网络或刷新登录信息")
        finally:
            self._emit("status", f"{account.username}：陪聊已停止")

    def stop(self, _payload=None):
        self._stop.set()
        return self.status()

    def wait(self, timeout=None):
        for thread in self._threads:
            thread.join(timeout)
        if self._test_thread is not None:
            self._test_thread.join(timeout)

    def test(self, payload):
        config = AIConfig.from_value((payload or {}).get("ai_chat") or {})
        config.selected()
        with self._lock:
            if self._test_busy:
                raise ValueError("API 测试正在进行")
            self._test_busy = True
            self._test_result = None
            self._test_thread = threading.Thread(target=self._run_test, args=(config,), daemon=True)
            self._test_thread.start()
        return {"started": True}

    def _run_test(self, config):
        provider = None
        try:
            provider = create_provider(config)
            text = provider.reply([{"role": "user", "content": "请用一句话打个招呼，并说明你是 AI。"}])
            result = {"ok": True, "text": text}
        except (ValueError, ProviderError) as exc:
            result = {"ok": False, "text": str(exc)}
        except Exception:
            result = {"ok": False, "text": "API 测试失败，请检查服务配置"}
        finally:
            if provider is not None:
                with suppress(Exception):
                    provider.close()
        with self._lock:
            self._test_result = result
            self._test_busy = False
