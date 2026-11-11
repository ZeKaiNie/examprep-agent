"""Gemini ``streamGenerateContent``（SSE）（R3.3）。

协议选项：``api_version``（默认 v1beta）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

from ..accumulator import StreamAccumulator
from ..errors import ServerError
from ..events import StreamEvent
from ..registry import ModelInfo
from ..types import (
    AssistantMessage,
    LLMRequest,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from ._http import as_dict, as_list, loads, map_stream_error, sse_post
from .base import HTTPProvider, tool_result_text

_FINISH: dict[str, StopReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "LANGUAGE": "content_filter",
}
_RETRYABLE_FINISH = frozenset({"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"})

THINKING_BUDGET = {"off": 0, "low": 1024, "medium": 8192, "high": 24576}

# Gemini Schema 支持的 OpenAPI 子集
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "description",
        "nullable",
        "enum",
        "properties",
        "required",
        "items",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "anyOf",
        "propertyOrdering",
        "minProperties",
        "maxProperties",
    }
)


def sanitize_schema(schema: Any) -> Any:
    """把 JSON Schema 转成 Gemini 接受的子集：去掉 $defs/title/default/additionalProperties，处理可空类型与 const。"""
    if isinstance(schema, list):
        return [sanitize_schema(x) for x in cast(list[Any], schema)]
    if not isinstance(schema, dict):
        return schema
    src = cast(dict[str, Any], schema)
    # Optional[X] 在 Pydantic 里是 anyOf: [X, {type: null}]
    any_of = src.get("anyOf")
    if isinstance(any_of, list):
        variants = [as_dict(v) for v in cast(list[Any], any_of)]
        non_null = [v for v in variants if v.get("type") != "null"]
        if len(non_null) == 1 and len(non_null) < len(variants):
            merged = {**{k: v for k, v in src.items() if k != "anyOf"}, **non_null[0]}
            out = as_dict(sanitize_schema(merged))
            out["nullable"] = True
            return out
    out: dict[str, Any] = {}
    for key, value in src.items():
        if key == "const":
            out["enum"] = [value]
        elif key == "type" and isinstance(value, list):
            types = [t for t in cast(list[Any], value) if t != "null"]
            out["type"] = types[0] if types else "string"
            if len(types) < len(cast(list[Any], value)):
                out["nullable"] = True
        elif key == "properties":
            out[key] = {k: sanitize_schema(v) for k, v in as_dict(value).items()}
        elif key in _SCHEMA_KEYS:
            out[key] = sanitize_schema(value)
    return out


def _user_parts(content: list[Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for b in content:
        if isinstance(b, TextBlock):
            if b.text:
                parts.append({"text": b.text})
        else:
            parts.append({"inlineData": {"mimeType": b.media_type, "data": b.data}})
    return parts


def _model_parts(msg: AssistantMessage) -> list[dict[str, Any]]:
    same = msg.protocol == "gemini"
    parts: list[dict[str, Any]] = []
    for b in msg.content:
        part: dict[str, Any] | None = None
        if isinstance(b, ThinkingBlock):
            if same and b.signature and not b.redacted:
                part = {"text": b.text, "thought": True}
        elif isinstance(b, TextBlock):
            if b.text or (same and b.signature):
                part = {"text": b.text}
        else:
            assert isinstance(b, ToolCallBlock)
            part = {"functionCall": {"name": b.name, "args": b.arguments}}
        if part is None:
            continue
        if same and b.signature:
            part["thoughtSignature"] = b.signature
        parts.append(part)
    return parts


def _function_response(msg: ToolResultMessage) -> dict[str, Any]:
    key = "error" if msg.is_error else "content"
    return {"functionResponse": {"name": msg.tool_name, "response": {key: tool_result_text(msg)}}}


def convert_contents(request: LLMRequest) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in request.conversation:
        if isinstance(m, UserMessage):
            role, parts = "user", _user_parts(list(m.content))
        elif isinstance(m, AssistantMessage):
            role, parts = "model", _model_parts(m)
        else:
            assert isinstance(m, ToolResultMessage)
            role, parts = "user", [_function_response(m)]
        if not parts:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})
    return out


def convert_usage(raw: dict[str, Any]) -> Usage:
    prompt = int(raw.get("promptTokenCount") or 0)
    cached = int(raw.get("cachedContentTokenCount") or 0)
    candidates = int(raw.get("candidatesTokenCount") or 0)
    thoughts = int(raw.get("thoughtsTokenCount") or 0)
    return Usage(
        input_tokens=max(0, prompt - cached),
        output_tokens=candidates + thoughts,
        cache_read_tokens=cached,
        reasoning_tokens=thoughts,
    )


class GeminiProvider(HTTPProvider):
    protocol = "gemini"

    def build_body(self, request: LLMRequest, model: ModelInfo) -> dict[str, Any]:
        body: dict[str, Any] = {"contents": convert_contents(request)}
        systems = [s.content for s in request.system_messages if s.content]
        if systems:
            body["systemInstruction"] = {"parts": [{"text": s} for s in systems]}
        if request.tools:
            decls: list[dict[str, Any]] = []
            for t in request.tools:
                decl: dict[str, Any] = {"name": t.name, "description": t.description}
                params = as_dict(sanitize_schema(t.parameters))
                if as_dict(params.get("properties")):  # 空 properties 的 OBJECT 会被拒绝
                    decl["parameters"] = params
                decls.append(decl)
            body["tools"] = [{"functionDeclarations": decls}]
        gen: dict[str, Any] = {}
        if request.max_tokens is not None:
            gen["maxOutputTokens"] = request.max_tokens
        if request.temperature is not None:
            gen["temperature"] = request.temperature
        if request.reasoning and model.supports_reasoning:
            thinking: dict[str, Any] = {"thinkingBudget": THINKING_BUDGET[request.reasoning]}
            if request.reasoning != "off":
                thinking["includeThoughts"] = True
            gen["thinkingConfig"] = thinking
        if gen:
            body["generationConfig"] = gen
        return self._merge_extra(body, request)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        key = self._api_key()
        if key:
            headers["x-goog-api-key"] = key
        headers.update(self.config.headers)
        return headers

    async def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]:
        acc = StreamAccumulator(provider=self.name, model=model.id, protocol=self.protocol)
        version = str(self.config.options.get("api_version", "v1beta"))
        url = f"{self.config.base_url.rstrip('/')}/{version}/models/{model.id}:streamGenerateContent?alt=sse"
        started = False
        finish: StopReason | None = None
        n_calls = 0
        async for sse in sse_post(
            self._client,
            url,
            body=self.build_body(request, model),
            headers=self._headers(),
            provider=self.name,
            model=model.id,
        ):
            obj = as_dict(loads(sse.data))
            if not obj:
                continue
            if "error" in obj:
                raise map_stream_error(as_dict(obj["error"]), provider=self.name, model=model.id)
            if not started:
                started = True
                yield acc.start()
            usage = obj.get("usageMetadata")
            if isinstance(usage, dict):
                acc.usage = convert_usage(as_dict(usage))
            block_reason = as_dict(obj.get("promptFeedback")).get("blockReason")
            if isinstance(block_reason, str) and block_reason:
                finish = "content_filter"
            response_id = str(obj.get("responseId") or uuid.uuid4().hex[:12])
            candidates = as_list(obj.get("candidates"))
            if not candidates:
                continue
            cand = as_dict(candidates[0])
            for raw in as_list(as_dict(cand.get("content")).get("parts")):
                part = as_dict(raw)
                sig = part.get("thoughtSignature")
                signature = sig if isinstance(sig, str) and sig else None
                fc = part.get("functionCall")
                if isinstance(fc, dict):
                    call = as_dict(fc)
                    key = ("fc", n_calls)
                    call_id = str(call.get("id") or f"{response_id}_{n_calls}")
                    n_calls += 1
                    yield acc.tool_start(key, call_id, str(call.get("name") or ""), signature=signature)
                    delta = acc.set_tool_arguments(key, as_dict(call.get("args")))
                    if delta is not None:
                        yield delta
                    end = acc.tool_end(key)
                    if end is not None:
                        yield end
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    continue
                if part.get("thought") is True:
                    ev = acc.thinking(text, signature=signature)
                    if ev is not None:
                        yield ev
                else:
                    ev = acc.text(text)
                    if ev is not None:
                        yield ev
                    if signature is not None:
                        acc.set_signature(signature)
            reason = cand.get("finishReason")
            if isinstance(reason, str) and reason:
                if reason in _RETRYABLE_FINISH:
                    raise ServerError(f"Gemini finishReason={reason}", provider=self.name, model=model.id)
                finish = _FINISH.get(reason, "stop")
        if finish is None:
            raise ServerError("流在 finishReason 之前意外结束", provider=self.name, model=model.id)
        yield acc.finish(finish)
