"""三种协议适配器。"""

from __future__ import annotations

import httpx

from ..config import ProviderConfig
from ._http import QuotaExceededError, map_http_error, map_stream_error, parse_retry_after
from .anthropic import AnthropicProvider
from .base import HTTPProvider, Provider
from .gemini import GeminiProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "HTTPProvider",
    "OpenAICompatProvider",
    "Provider",
    "QuotaExceededError",
    "build_provider",
    "map_http_error",
    "map_stream_error",
    "parse_retry_after",
]

_PROTOCOLS: dict[str, type[HTTPProvider]] = {
    "openai": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def build_provider(
    config: ProviderConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client: httpx.AsyncClient | None = None,
) -> Provider:
    cls = _PROTOCOLS.get(config.protocol)
    if cls is None:
        raise ValueError(
            f"无法从配置构建协议 {config.protocol!r} 的 provider（fake 请直接实例化 FakeProvider）"
        )
    return cls(config, transport=transport, client=client)
