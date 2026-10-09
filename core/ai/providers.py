"""Provider 注册表；新增协议可注册工厂，无需改回复引擎。"""
from __future__ import annotations

from typing import Callable, Protocol
from core.ai.config import AIConfig, ProviderConfig


class ChatProvider(Protocol):
    def reply(self, messages: list[dict]) -> str: ...
    def close(self) -> None: ...


class ProviderError(RuntimeError):
    """可向用户显示的错误，不带请求内容、密钥或服务端原始响应。"""


class OpenAICompatibleProvider:
    def __init__(self, provider: ProviderConfig, config: AIConfig):
        from openai import OpenAI
        self.provider = provider
        self.config = config
        self.client = OpenAI(
            api_key=provider.api_key, base_url=provider.base_url,
            timeout=config.request_timeout, max_retries=0,
        )

    def reply(self, messages):
        from openai import APIConnectionError, APIStatusError, APITimeoutError
        kwargs = {}
        if self.provider.kind == "deepseek":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            response = self.client.chat.completions.create(
                model=self.provider.model, messages=messages,
                max_tokens=self.config.max_tokens, stream=False, **kwargs,
            )
        except APITimeoutError:
            raise ProviderError("AI 请求超时，请稍后重试") from None
        except APIConnectionError:
            raise ProviderError("无法连接 AI 服务，请检查 API 地址和网络") from None
        except APIStatusError as exc:
            tips = {401: "API Key 无效", 402: "AI 账户余额不足", 403: "AI 服务拒绝访问",
                    404: "API 地址或模型名称不正确", 429: "AI 请求过于频繁或额度不足"}
            raise ProviderError(tips.get(exc.status_code, f"AI 服务返回 HTTP {exc.status_code}")) from None
        except Exception:
            raise ProviderError("AI 返回格式异常") from None
        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            content = None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("AI 返回了空回复")
        return content.strip()[:self.config.max_reply_chars]

    def close(self):
        self.client.close()


_FACTORIES: dict[str, Callable] = {
    "deepseek": OpenAICompatibleProvider,
    "openai-compatible": OpenAICompatibleProvider,
}


def register_provider(kind: str, factory: Callable) -> None:
    _FACTORIES[kind] = factory


def create_provider(config: AIConfig) -> ChatProvider:
    provider = config.selected()
    factory = _FACTORIES.get(provider.kind)
    if factory is None:
        raise ValueError(f"尚未支持 AI 接口类型：{provider.kind}")
    return factory(provider, config)
