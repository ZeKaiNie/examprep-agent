"""OpenAI 兼容 Chat Completions（流式）（R3.1）。

覆盖 OpenAI、DeepSeek、通义、GLM、Kimi、硅基流动、OpenRouter、Ollama、LM Studio、vLLM、llama.cpp。

协议选项（``ProviderConfig.options``）：
- ``max_tokens_field``：``max_tokens``（默认）或 ``max_completion_tokens``（openai 默认）
- ``stream_usage``：是否发送 ``stream_options.include_usage``，默认 True
- ``send_reasoning``：回传同一 provider 产生的历史推理内容（``reasoning_content``），默认 False（deepseek 预设为 True）
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from ..accumulator import StreamAccumulator
from ..errors import ServerError
from ..events import StreamEvent
from ..registry import ModelInfo
from ..types import (
    AssistantMessage,
    LLMRequest,
    StopReason,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from ._http import as_dict, as_list, loads, map_stream_error, sse_post
from .base import HTTPProvider, tool_result_text

_FINISH: dict[str, StopReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filter",
}


def _user_content(msg: UserMessage) -> str | list[dict[str, Any]]:
    if all(isinstance(b, TextBlock) for b in msg.content):
        return "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    parts: list[dict[str, Any]] = []
    for b in msg.content:
        if isinstance(b, TextBlock):
            parts.append({"type": "text", "text": b.text})
        else:
            parts.append({"type": "image_url", "image_url": {"url": f"data:{b.media_type};base64,{b.data}"}})
    return parts


def _assistant(msg: AssistantMessage, *, reasoning_from: str | None) -> dict[str, Any] | None:
    text = msg.text
    calls = msg.tool_calls
    out: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)},
            }
            for c in calls
        ]
    if reasoning_from is not None and msg.protocol == "openai" and msg.provider == reasoning_from:
        reasoning = "".join(b.text for b in msg.content if isinstance(b, ThinkingBlock) and not b.redacted)
        if reasoning:
            out["reasoning_content"] = reasoning
    if not text and not calls:
        return None  # 空的助手消息（例如被取消）不发送
    return out


def convert_messages(request: LLMRequest, *, reasoning_from: str | None = None) -> list[dict[str, Any]]:
    """``reasoning_from``：回传该 provider 产生的推理内容；None 表示不回传。"""
    out: list[dict[str, Any]] = [
        {"role": "system", "content": m.content} for m in request.messages if isinstance(m, SystemMessage)
    ]
    for m in request.conversation:
        if isinstance(m, UserMessage):
            out.append({"role": "user", "content": _user_content(m)})
        elif isinstance(m, AssistantMessage):
            item = _assistant(m, reasoning_from=reasoning_from)
            if item is not None:
                out.append(item)
        else:
            assert isinstance(m, ToolResultMessage)
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": tool_result_text(m)})
    return out


def convert_usage(raw: dict[str, Any]) -> Usage:
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    details = as_dict(raw.get("prompt_tokens_details"))
    out_details = as_dict(raw.get("completion_tokens_details"))
    reasoning = int(out_details.get("reasoning_tokens") or 0)
    if "prompt_cache_hit_tokens" in raw or "prompt_cache_miss_tokens" in raw:  # DeepSeek
        hit = int(raw.get("prompt_cache_hit_tokens") or 0)
        miss_raw = raw.get("prompt_cache_miss_tokens")
        miss = int(miss_raw) if miss_raw is not None else max(0, prompt - hit)
        return Usage(
            input_tokens=miss, output_tokens=completion, cache_read_tokens=hit, reasoning_tokens=reasoning
        )
    cached = int(details.get("cached_tokens") or 0)
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=completion,
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )


class OpenAICompatProvider(HTTPProvider):
    protocol = "openai"

    def build_body(self, request: LLMRequest, model: ModelInfo) -> dict[str, Any]:
        opts = self.config.options
        body: dict[str, Any] = {
            "model": model.id,
            "messages": convert_messages(
                request, reasoning_from=self.name if opts.get("send_reasoning", False) else None
            ),
            "stream": True,
        }
        if opts.get("stream_usage", True):
            body["stream_options"] = {"include_usage": True}
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in request.tools
            ]
        if request.max_tokens is not None:
            default_field = "max_completion_tokens" if self.name == "openai" else "max_tokens"
            body[str(opts.get("max_tokens_field", default_field))] = request.max_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.reasoning and request.reasoning != "off" and model.supports_reasoning:
            body["reasoning_effort"] = request.reasoning
        return self._merge_extra(body, request)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self.config.headers)
        return headers

    async def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]:
        acc = StreamAccumulator(provider=self.name, model=model.id, protocol=self.protocol)
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        started = False
        finish: StopReason | None = None
        done = False
        active: dict[int, tuple[int, str]] = {}
        async for sse in sse_post(
            self._client,
            url,
            body=self.build_body(request, model),
            headers=self._headers(),
            provider=self.name,
            model=model.id,
        ):
            if sse.data.strip() == "[DONE]":
                done = True
                break
            obj = as_dict(loads(sse.data))
            if not obj:
                continue
            if "error" in obj:
                raise map_stream_error(as_dict(obj["error"]), provider=self.name, model=model.id)
            if not started:
                started = True
                yield acc.start()
            usage = obj.get("usage")
            if isinstance(usage, dict):
                acc.usage = convert_usage(as_dict(usage))
            for choice in as_list(obj.get("choices")):
                ch = as_dict(choice)
                if int(ch.get("index") or 0) != 0:
                    continue
                for ev in self._delta(acc, active, as_dict(ch.get("delta"))):
                    yield ev
                reason = ch.get("finish_reason")
                if isinstance(reason, str) and reason:
                    finish = _FINISH.get(reason, "stop")
                    for ev in acc.end_all_tools():
                        yield ev
        if finish is None and not done:
            raise ServerError("流在完成前意外结束", provider=self.name, model=model.id)
        if not started:
            yield acc.start()
        yield acc.finish(finish or "stop")

    @staticmethod
    def _delta(
        acc: StreamAccumulator, active: dict[int, tuple[int, str]], delta: dict[str, Any]
    ) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        for key in ("reasoning_content", "reasoning"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                ev = acc.thinking(value)
                if ev is not None:
                    out.append(ev)
                break
        content = delta.get("content")
        if isinstance(content, str) and content:
            ev = acc.text(content)
            if ev is not None:
                out.append(ev)
        for raw in as_list(delta.get("tool_calls")):
            tc = as_dict(raw)
            idx = int(tc.get("index") or 0)
            fn = as_dict(tc.get("function"))
            new_id = tc.get("id")
            current = active.get(idx)
            # 部分服务对多个调用都给 index 0，但 id 不同：视为新调用
            if current is not None and isinstance(new_id, str) and new_id and new_id != current[1]:
                end = acc.tool_end(current)
                if end is not None:
                    out.append(end)
                current = None
            if current is None or not acc.has_tool(current):
                call_id = str(new_id or f"call_{idx}_{len(active)}")
                current = (idx, call_id)
                active[idx] = current
                out.append(acc.tool_start(current, call_id, str(fn.get("name") or "")))
            args = fn.get("arguments")
            if isinstance(args, str):
                ev = acc.tool_args(current, args)
                if ev is not None:
                    out.append(ev)
            elif isinstance(args, dict):  # 少数本地服务直接给对象
                ev = acc.set_tool_arguments(current, as_dict(args))
                if ev is not None:
                    out.append(ev)
        return out
