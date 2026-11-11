"""Provider 协议与 HTTP provider 基类。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx

from ..config import ProviderConfig
from ..events import StreamEvent
from ..registry import ModelInfo
from ..types import LLMRequest, ToolResultMessage

IMAGE_PLACEHOLDER = "[图片已省略]"


@runtime_checkable
class Provider(Protocol):
    name: str
    config: ProviderConfig

    def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]: ...

    async def aclose(self) -> None: ...


class HTTPProvider:
    """持有一个 httpx.AsyncClient；子类实现 ``stream``。"""

    protocol: ClassVar[str] = ""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = config.name
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(config.timeout, connect=config.connect_timeout),
        )

    def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError

    def _api_key(self) -> str | None:
        return self.config.resolve_api_key()

    def _merge_extra(self, body: dict[str, Any], request: LLMRequest) -> dict[str, Any]:
        body.update(request.extra)
        return body

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def tool_result_text(msg: ToolResultMessage) -> str:
    """工具结果转成纯文本（不支持图片的协议用）。"""
    parts: list[str] = []
    for b in msg.content:
        parts.append(b.text if b.type == "text" else IMAGE_PLACEHOLDER)
    return "\n".join(parts)
