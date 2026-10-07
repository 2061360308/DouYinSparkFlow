"""消息去重与对话上下文；不依赖浏览器，发送成功后才提交助手回复。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import time

from core.ai.config import AIConfig


@dataclass
class PendingReply:
    conv_id: str
    ids: tuple[str, ...]
    text: str
    latest_at: float
    is_group: bool = False


class ReplyEngine:
    def __init__(self, config: AIConfig, started_at=None):
        self.config = config
        self.started_at = time.time() if started_at is None else started_at
        self.seen = OrderedDict()
        self.history = {}
        self.last_reply = {}
        self.retry_after = {}
        self.sender_labels = {}

    def consume(self, pending):
        for mid in pending.ids:
            self.seen[(pending.conv_id, mid)] = True
        while len(self.seen) > 4096:
            self.seen.popitem(last=False)

    def prepare(self, conv_id, rows, now=None, is_group=False):
        now = time.time() if now is None else now
        if now < self.retry_after.get(conv_id, 0):
            return None
        if now - self.last_reply.get(conv_id, 0) < self.config.cooldown:
            return None
        ordered = sorted(rows, key=lambda r: (r.get("created_at", 0), r.get("id", "")))
        # 手动或其他任务已经回复过的消息不再补发 AI 回复。
        own_at = max((r.get("created_at", 0) for r in ordered if r.get("from_me")), default=0)
        incoming = [r for r in ordered if r.get("id") and not r.get("from_me")
                    and r.get("created_at", 0) >= self.started_at
                    and r.get("created_at", 0) > own_at
                    and (conv_id, str(r["id"])) not in self.seen and r.get("text")]
        if not incoming:
            return None
        texts = []
        labels = self.sender_labels.setdefault(conv_id, {}) if is_group else {}
        for message in incoming:
            if is_group:
                sender = str(message.get("sender_id") or "")
                if sender and sender not in labels:
                    labels[sender] = f"成员{len(labels) + 1}"
                label = labels.get(sender, "未识别成员")
                texts.append(f"[{label}] {message['text']}")
            else:
                texts.append(message["text"])
        return PendingReply(conv_id, tuple(str(r["id"]) for r in incoming),
                            "\n".join(texts)[-8000:],
                            incoming[-1]["created_at"], is_group)

    def messages(self, pending):
        prompt = self.config.system_prompt
        if pending.is_group:
            prompt += ("\n当前是多人群聊，[成员N] 是同一成员的固定标签，"
                       "[未识别成员] 的消息可能来自不同人。根据发言顺序和成员标签理解对话，"
                       "以一条简短消息自然参与聊天，不把不同成员当成同一个人，"
                       "不要在回复中输出这些内部标签。")
        return [{"role": "system", "content": prompt}] + \
            self.history.get(pending.conv_id, []) + [{"role": "user", "content": pending.text}]

    def commit(self, pending, reply, now=None):
        self.consume(pending)
        history = self.history.setdefault(pending.conv_id, [])
        history.extend([{"role": "user", "content": pending.text},
                        {"role": "assistant", "content": reply}])
        self.history[pending.conv_id] = history[-self.config.context_turns * 2:]
        self.last_reply[pending.conv_id] = time.time() if now is None else now

    def failed(self, pending, now=None):
        # API 失败允许退避重试；发送无回执则由调用方 consume，避免重复发消息。
        self.retry_after[pending.conv_id] = (time.time() if now is None else now) + 60
