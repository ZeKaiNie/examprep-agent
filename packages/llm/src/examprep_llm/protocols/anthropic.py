"""Anthropic Messages API（流式）（R3.2）。

协议选项：``version``（anthropic-version 头，默认 2023-06-01）、``beta``（anthropic-beta 头）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..accumulator import StreamAccumulator
from ..errors import ServerError
from ..events import StreamEvent
from ..registry import ModelInfo
from ..types import (
    AssistantMessage,
    ImageBlock,
    LLMRequest,
    StopReason,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from ._http import as_dict, loads, map_stream_error, sse_post
from .base import HTTPProvider

_STOP: dict[str, StopReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_use",
    "refusal": "content_filter",
}

THINKING_BUDGET = {"low": 2048, "medium": 8192, "high": 16384}
_CACHE = {"type": "ephemeral"}


def _image(b: ImageBlock) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": b.media_type, "data": b.data}}


def _user_blocks(content: list[TextBlock | ImageBlock]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in content:
        if isinstance(b, TextBlock):
            if b.text:
                out.append({"type": "text", "text": b.text})
        else:
            out.append(_image(b))
    return out


def _assistant_blocks(msg: AssistantMessage) -> list[dict[str, Any]]:
    same = msg.protocol == "anthropic"
    out: list[dict[str, Any]] = []
    for b in msg.content:
        if isinstance(b, ThinkingBlock):
            # 推理块必须原样带签名回传；跨协议或无签名的推理内容丢弃
            if not same:
                continue
            if b.redacted:
                out.append({"type": "redacted_thinking", "data": b.text})
            elif b.signature:
                out.append({"type": "thinking", "thinking": b.text, "signature": b.signature})
        elif isinstance(b, TextBlock):
            if b.text:
                out.append({"type": "text", "text": b.text})
        else:
            assert isinstance(b, ToolCallBlock)
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.arguments})
    return out


def _tool_result(msg: ToolResultMessage) -> dict[str, Any]:
    content = _user_blocks(msg.content) or [{"type": "text", "text": "（空）"}]
    item: dict[str, Any] = {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": content}
    if msg.is_error:
        item["is_error"] = True
    return item


def convert_messages(request: LLMRequest) -> list[dict[str, Any]]:
    """转换对话；相邻同角色消息合并（tool_result 归入 user）。"""
    out: list[dict[str, Any]] = []
    for m in request.conversation:
        if isinstance(m, UserMessage):
            role, blocks = "user", _user_blocks(m.content)
        elif isinstance(m, AssistantMessage):
            role, blocks = "assistant", _assistant_blocks(m)
        else:
            assert isinstance(m, ToolResultMessage)
            role, blocks = "user", [_tool_result(m)]
        if not blocks:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return out


def convert_system(systems: list[SystemMessage], *, cache: bool) -> list[dict[str, Any]]:
    kept = [s for s in systems if s.content]
    blocks: list[dict[str, Any]] = [{"type": "text", "text": s.content} for s in kept]
    if cache:
        cacheable = [i for i, s in enumerate(kept) if s.cacheable]
        if cacheable:
            blocks[cacheable[-1]]["cache_control"] = dict(_CACHE)
    return blocks


def _update_usage(usage: Usage, raw: dict[str, Any]) -> Usage:
    data = usage.model_dump()
    mapping = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read_tokens",
        "cache_creation_input_tokens": "cache_write_tokens",
    }
    for src, dst in mapping.items():
        value = raw.get(src)
        if isinstance(value, int):
            data[dst] = value
    return Usage.model_validate(data)


class AnthropicProvider(HTTPProvider):
    protocol = "anthropic"

    def build_body(self, request: LLMRequest, model: ModelInfo) -> dict[str, Any]:
        cache = model.supports_prompt_cache
        systems = request.system_messages
        max_tokens = request.max_tokens or model.max_output
        body: dict[str, Any] = {
            "model": model.id,
            "max_tokens": max_tokens,
            "messages": convert_messages(request),
            "stream": True,
        }
        system = convert_system(systems, cache=cache)
        if system:
            body["system"] = system
        if request.tools:
            tools: list[dict[str, Any]] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
            if cache and any(s.cacheable for s in systems):
                tools[-1]["cache_control"] = dict(_CACHE)
            body["tools"] = tools
        thinking = False
        if request.reasoning and request.reasoning != "off" and model.supports_reasoning:
            budget = min(THINKING_BUDGET[request.reasoning], max_tokens // 2)
            if budget >= 1024:
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
                thinking = True
        if request.temperature is not None and not thinking:  # 开启 thinking 时不允许设置 temperature
            body["temperature"] = request.temperature
        return self._merge_extra(body, request)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "anthropic-version": str(self.config.options.get("version", "2023-06-01")),
        }
        beta = self.config.options.get("beta")
        if beta:
            headers["anthropic-beta"] = str(beta)
        key = self._api_key()
        if key:
            headers["x-api-key"] = key
        headers.update(self.config.headers)
        return headers

    async def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]:
        acc = StreamAccumulator(provider=self.name, model=model.id, protocol=self.protocol)
        url = self.config.base_url.rstrip("/") + "/v1/messages"
        started = False
        stop: StopReason = "stop"
        finished = False
        async for sse in sse_post(
            self._client,
            url,
            body=self.build_body(request, model),
            headers=self._headers(),
            provider=self.name,
            model=model.id,
        ):
            obj = as_dict(loads(sse.data))
            kind = str(obj.get("type") or sse.event)
            if kind == "error":
                raise map_stream_error(as_dict(obj.get("error")), provider=self.name, model=model.id)
            if kind == "ping" or not obj:
                continue
            if not started:
                started = True
                yield acc.start()
            if kind == "message_start":
                acc.usage = _update_usage(acc.usage, as_dict(as_dict(obj.get("message")).get("usage")))
            elif kind == "content_block_start":
                idx = obj.get("index")
                block = as_dict(obj.get("content_block"))
                btype = block.get("type")
                acc.close_block()
                if btype == "tool_use":
                    yield acc.tool_start(
                        idx, str(block.get("id") or f"toolu_{idx}"), str(block.get("name") or "")
                    )
                elif btype == "redacted_thinking":
                    acc.redacted_thinking(str(block.get("data") or ""))
                elif btype == "text":
                    ev = acc.text(str(block.get("text") or ""))
                    if ev is not None:
                        yield ev
                elif btype == "thinking":
                    ev = acc.thinking(str(block.get("thinking") or ""))
                    if ev is not None:
                        yield ev
            elif kind == "content_block_delta":
                idx = obj.get("index")
                delta = as_dict(obj.get("delta"))
                dtype = delta.get("type")
                ev: StreamEvent | None = None
                if dtype == "text_delta":
                    ev = acc.text(str(delta.get("text") or ""))
                elif dtype == "thinking_delta":
                    ev = acc.thinking(str(delta.get("thinking") or ""))
                elif dtype == "signature_delta":
                    acc.set_signature(str(delta.get("signature") or ""))
                elif dtype == "input_json_delta":
                    ev = acc.tool_args(idx, str(delta.get("partial_json") or ""))
                if ev is not None:
                    yield ev
            elif kind == "content_block_stop":
                idx = obj.get("index")
                if acc.has_tool(idx):
                    end = acc.tool_end(idx)
                    if end is not None:
                        yield end
                else:
                    acc.close_block()
            elif kind == "message_delta":
                reason = as_dict(obj.get("delta")).get("stop_reason")
                if isinstance(reason, str):
                    stop = _STOP.get(reason, "stop")
                acc.usage = _update_usage(acc.usage, as_dict(obj.get("usage")))
            elif kind == "message_stop":
                finished = True
                break
        if not finished:
            raise ServerError("流在 message_stop 之前意外结束", provider=self.name, model=model.id)
        yield acc.finish(stop)
