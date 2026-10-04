"""Python -> 页面的事件名，以及 browser_event 的 kind 集合。

集中在这里，避免字符串散落在 bridge / sessions 各处。
"""

from __future__ import annotations

# 事件名（bridge.emit 的第一参数 / 前端 on(event) 订阅的名字）
BROWSER_EVENT = "browser_event"

# browser_event 里 worker 会发的 kind 集合（其余一律不透传；与前端 switch 对应）
WORKER_KINDS = {
    "log",
    "status",
    "opened",
    "probe",
    "grabbed",
    "conversations",
    "conversation_progress",
    "error",
    "done",
}
