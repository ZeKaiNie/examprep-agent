"""HTTP 层公共逻辑：错误映射、retry-after 解析、SSE 请求封装（R3.4）。"""

from __future__ import annotations

import email.utils
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import httpx

from ..errors import (
    AuthenticationError,
    BadRequestError,
    ContextOverflowError,
    LLMError,
    LLMTimeoutError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from ..sse import SSEEvent, aiter_sse

# 各家“上下文超长”的错误文本（小写子串匹配）
_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "maximum context length",
    "context length",
    "context window",
    "prompt is too long",
    "input is too long",
    "too many tokens",
    "reduce the length",
    "exceeds the maximum number of tokens",
    "input token count",
    "available context size",
    "request_too_large",
)

_QUOTA_PATTERNS = ("insufficient_quota", "insufficient balance", "余额不足", "billing")


class QuotaExceededError(AuthenticationError):
    """额度或余额不足：不重试，但允许降级到其他 provider。"""

    kind = "quota"


def as_dict(value: Any) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_error(body: str) -> tuple[str, str]:
    """从错误响应体里取 (消息, 类型/状态/错误码)。兼容 OpenAI、Anthropic、Gemini 三种格式。"""
    data = loads(body)
    if isinstance(data, list) and data:  # Gemini 有时把错误包在数组里
        data = cast(list[Any], data)[0]
    err = as_dict(as_dict(data).get("error"))
    if err:
        message = str(err.get("message") or body)
        code = str(err.get("code") or "")
        kind = str(err.get("type") or err.get("status") or "")
        return message, " ".join(x for x in (kind, code) if x)
    text = body.strip()
    return (text[:500] if text else "（无响应体）"), ""


def parse_retry_after(
    headers: Mapping[str, str], body: str = "", *, now: float | None = None
) -> float | None:
    """解析 retry-after-ms、retry-after（秒或 HTTP 日期）以及 Gemini 的 RetryInfo.retryDelay。"""
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    ra = headers.get("retry-after")
    if ra:
        try:
            return max(0.0, float(ra))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(ra)
            except (TypeError, ValueError):
                dt = None
            if dt is not None:
                current = time.time() if now is None else now
                return max(0.0, dt.timestamp() - current)
    if body:
        data = loads(body)
        if isinstance(data, list) and data:
            data = cast(list[Any], data)[0]
        for detail in as_list(as_dict(as_dict(data).get("error")).get("details")):
            delay = as_dict(detail).get("retryDelay")
            if isinstance(delay, str):
                m = re.fullmatch(r"\s*([\d.]+)s\s*", delay)
                if m:
                    return float(m.group(1))
    return None


def _is_overflow(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in _OVERFLOW_PATTERNS)


def map_http_error(
    status: int,
    headers: Mapping[str, str],
    body: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> LLMError:
    message, kind = extract_error(body)
    kw: dict[str, Any] = {"status": status, "provider": provider, "model": model, "body": body[:2000]}
    probe = f"{kind} {message}"
    low = probe.lower()
    if status in (401, 403) or "api key not valid" in low or "api_key_invalid" in low:
        return AuthenticationError(message, **kw)
    if any(p in low for p in _QUOTA_PATTERNS) and status in (402, 429):
        return QuotaExceededError(message, **kw)
    if status == 402:
        return QuotaExceededError(message, **kw)
    if status == 429:
        return RateLimitError(message, retry_after=parse_retry_after(headers, body), **kw)
    if status == 413 or (400 <= status < 500 and _is_overflow(probe)):
        return ContextOverflowError(message, **kw)
    if status == 408:
        return LLMTimeoutError(message, **kw)
    if status >= 500 or status in (409, 425):
        return ServerError(message, **kw)
    return BadRequestError(message, **kw)


def map_stream_error(err: Mapping[str, Any], *, provider: str | None, model: str | None) -> LLMError:
    """流中途出现的错误对象（Anthropic ``event: error``、OpenAI 兼容的 ``{"error": ...}``）。"""
    message = str(err.get("message") or "stream error")
    kind = str(err.get("type") or err.get("status") or err.get("code") or "")
    kw: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "body": json.dumps(dict(err), ensure_ascii=False),
    }
    low = f"{kind} {message}".lower()
    if "rate_limit" in low or "resource_exhausted" in low:
        return RateLimitError(message, **kw)
    if "authentication" in low or "permission" in low:
        return AuthenticationError(message, **kw)
    if _is_overflow(low):
        return ContextOverflowError(message, **kw)
    if "invalid_request" in low or "invalid_argument" in low:
        return BadRequestError(message, **kw)
    return ServerError(message, **kw)


async def sse_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    body: dict[str, Any],
    headers: Mapping[str, str],
    provider: str,
    model: str,
) -> AsyncIterator[SSEEvent]:
    """POST 并逐个产出 SSE 事件；HTTP 状态错误与传输错误都映射为 :class:`LLMError`。"""
    try:
        async with client.stream("POST", url, json=body, headers=dict(headers)) as resp:
            if resp.status_code >= 400:
                raw = (await resp.aread()).decode("utf-8", "replace")
                raise map_http_error(resp.status_code, resp.headers, raw, provider=provider, model=model)
            async for ev in aiter_sse(resp):
                yield ev
    except httpx.TimeoutException as exc:
        raise LLMTimeoutError(f"请求超时：{type(exc).__name__}", provider=provider, model=model) from exc
    except httpx.TransportError as exc:
        raise NetworkError(f"网络错误：{type(exc).__name__}: {exc}", provider=provider, model=model) from exc
