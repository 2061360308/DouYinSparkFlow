"""界面、CLI 与聊天工作线程共享的配置。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from urllib.parse import urlsplit

DEFAULT_PROMPT = (
    "你是友善的 AI 陪聊助手，用自然、简短的中文与好友聊天。"
    "认真回应对方，通常回复一到三句话，不要频繁追问。"
    "被问到身份时说明自己是 AI，不冒充账号主人。"
)


@dataclass
class ProviderConfig:
    id: str = "deepseek"
    name: str = "DeepSeek"
    kind: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key: str = field(default="", repr=False)


@dataclass
class AIConfig:
    providers: list[ProviderConfig] = field(default_factory=lambda: [ProviderConfig()])
    active_provider: str = "deepseek"
    system_prompt: str = DEFAULT_PROMPT
    poll_interval: int = 5
    cooldown: int = 15
    context_turns: int = 10
    max_tokens: int = 512
    max_reply_chars: int = 500
    request_timeout: int = 30

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_value(cls, value):
        if isinstance(value, str):
            value = json.loads(value or "{}")
        if not isinstance(value, dict):
            raise ValueError("AI_CHAT 必须是 JSON 对象")
        default = cls()
        providers = value.get("providers", default.to_dict()["providers"])
        if not isinstance(providers, list) or not providers:
            raise ValueError("至少需要一个 AI 服务")
        fields = ProviderConfig.__dataclass_fields__
        default.providers = [
            ProviderConfig(**{k: str(v or "").strip() for k, v in p.items() if k in fields})
            for p in providers if isinstance(p, dict)
        ]
        for key in ("active_provider", "system_prompt"):
            if key in value:
                setattr(default, key, str(value[key] or ""))
        limits = {
            "poll_interval": (2, 120), "cooldown": (0, 3600),
            "context_turns": (1, 30), "max_tokens": (32, 4096),
            "max_reply_chars": (20, 2000), "request_timeout": (5, 120),
        }
        for key, (lo, hi) in limits.items():
            if key in value:
                number = int(value[key])
                if not lo <= number <= hi:
                    raise ValueError(f"{key} 应在 {lo} 到 {hi} 之间")
                setattr(default, key, number)
        return default

    def selected(self):
        ids = [p.id for p in self.providers]
        if len(ids) != len(set(ids)) or any(not i for i in ids):
            raise ValueError("AI 服务标识不能为空或重复")
        provider = next((p for p in self.providers if p.id == self.active_provider), None)
        if provider is None:
            raise ValueError("请选择一个 AI 服务")
        parsed = urlsplit(provider.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("API 地址应是 http:// 或 https:// 开头的服务地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 地址不能包含账号、密码、查询参数或片段")
        if not provider.api_key:
            raise ValueError("请填写所选 AI 服务的 API Key")
        if not provider.model:
            raise ValueError("请填写模型名称")
        if not self.system_prompt.strip():
            raise ValueError("陪聊角色提示词不能为空")
        return provider
