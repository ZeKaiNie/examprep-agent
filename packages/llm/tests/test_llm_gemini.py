from __future__ import annotations

from typing import Any

import pytest

from examprep_llm import (
    AssistantMessage,
    ImageBlock,
    LLMRequest,
    ModelRegistry,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
    provider_config,
)
from examprep_llm.errors import AuthenticationError, ContextOverflowError, RateLimitError, ServerError
from examprep_llm.protocols import GeminiProvider
from examprep_llm.protocols.gemini import sanitize_schema
from examprep_llm.testing import sse_response, sse_text

REG = ModelRegistry()
FLASH = REG.get("gemini", "gemini-2.5-flash", "gemini")
WEATHER = ToolSpec(
    name="get_weather",
    description="查天气",
    parameters={
        "type": "object",
        "title": "Params",
        "additionalProperties": False,
        "properties": {"city": {"type": "string", "title": "City", "default": "北京"}},
        "required": ["city"],
    },
)
NO_ARGS = ToolSpec(name="now", description="现在几点", parameters={"type": "object", "properties": {}})


def make(rec: Any) -> GeminiProvider:
    return GeminiProvider(provider_config("gemini", api_key="gk"), transport=rec.transport)


async def collect(p: GeminiProvider, r: LLMRequest) -> list[Any]:
    return [ev async for ev in p.stream(r, FLASH)]


def req(*msgs: Any, **kw: Any) -> LLMRequest:
    return LLMRequest(messages=list(msgs) or [UserMessage(content="北京天气？")], **kw)  # type: ignore[arg-type]


async def test_function_call_stream(replay: Any) -> None:
    rec = replay("gemini_tool_call.sse")
    events = await collect(make(rec), req(tools=[WEATHER, NO_ARGS], reasoning="low"))
    assert [e.type for e in events] == [
        "start",
        "thinking_delta",
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
        "done",
    ]
    msg = events[-1].message
    assert msg.stop_reason == "tool_use"
    call = msg.tool_calls[0]
    assert (call.id, call.name, call.arguments, call.signature) == (
        "resp_g1_0",
        "get_weather",
        {"city": "北京"},
        "CiQBVKhc7g==",
    )
    assert msg.thinking.startswith("**查询天气**")
    u = msg.usage
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.reasoning_tokens) == (50, 30, 40, 25)

    request = rec.requests[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    )
    assert request.headers["x-goog-api-key"] == "gk"
    body = rec.body()
    decls = body["tools"][0]["functionDeclarations"]
    assert decls[0]["parameters"] == {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    assert "parameters" not in decls[1]  # 空 properties 省略
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024, "includeThoughts": True}


async def test_text_stream(replay: Any) -> None:
    for size in (2, 11):
        msg = (await collect(make(replay("gemini_text.sse", chunk_size=size)), req()))[-1].message
        assert msg.text == "北京今天晴，25 度。"
        assert msg.stop_reason == "stop"
        assert msg.usage.input_tokens == 140


def test_convert_contents_and_signatures() -> None:
    p = GeminiProvider(provider_config("gemini"))
    r = req(
        SystemMessage(content="你是助教"),
        SystemMessage(content="本轮任务"),
        UserMessage(content=[TextBlock(text="图"), ImageBlock(media_type="image/jpeg", data="ZZ")]),
        AssistantMessage(
            content=[
                ThinkingBlock(text="想", signature="ts"),
                ThinkingBlock(text="无签名丢弃"),
                TextBlock(text="", signature="only-sig"),
                ToolCallBlock(id="x", name="get_weather", arguments={"city": "北京"}, signature="fs"),
                ToolCallBlock(id="y", name="now", arguments={}),
            ],
            protocol="gemini",
        ),
        ToolResultMessage(tool_call_id="x", tool_name="get_weather", content="晴"),
        ToolResultMessage(tool_call_id="y", tool_name="now", content="出错了", is_error=True),
        AssistantMessage(content=[TextBlock(text="别家", signature="drop")], protocol="anthropic"),
        max_tokens=256,
        temperature=0.1,
        reasoning="off",
    )
    body = p.build_body(r, FLASH)
    assert body["systemInstruction"] == {"parts": [{"text": "你是助教"}, {"text": "本轮任务"}]}
    contents = body["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "user", "model"]
    assert contents[0]["parts"][1] == {"inlineData": {"mimeType": "image/jpeg", "data": "ZZ"}}
    assert contents[1]["parts"] == [
        {"text": "想", "thought": True, "thoughtSignature": "ts"},
        {"text": "", "thoughtSignature": "only-sig"},
        {"functionCall": {"name": "get_weather", "args": {"city": "北京"}}, "thoughtSignature": "fs"},
        {"functionCall": {"name": "now", "args": {}}},
    ]
    assert contents[2]["parts"] == [
        {"functionResponse": {"name": "get_weather", "response": {"content": "晴"}}},
        {"functionResponse": {"name": "now", "response": {"error": "出错了"}}},
    ]
    assert contents[3]["parts"] == [{"text": "别家"}]  # 跨协议不带签名
    assert body["generationConfig"] == {
        "maxOutputTokens": 256,
        "temperature": 0.1,
        "thinkingConfig": {"thinkingBudget": 0},
    }


def test_sanitize_schema() -> None:
    schema = {
        "type": "object",
        "$defs": {"X": {}},
        "properties": {
            "a": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "A"},
            "b": {"type": ["string", "null"]},
            "c": {"const": "fixed"},
            "d": {"type": "array", "items": {"type": "string", "title": "D"}},
            "e": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        },
    }
    assert sanitize_schema(schema) == {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "nullable": True},
            "b": {"type": "string", "nullable": True},
            "c": {"enum": ["fixed"]},
            "d": {"type": "array", "items": {"type": "string"}},
            "e": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        },
    }


async def test_finish_reasons_and_block(replay: Any) -> None:
    text = sse_text([{"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "SAFETY"}]}])
    assert (await collect(make(replay(text)), req()))[-1].message.stop_reason == "content_filter"

    text = sse_text([{"promptFeedback": {"blockReason": "SAFETY"}}])
    assert (await collect(make(replay(text)), req()))[-1].message.stop_reason == "content_filter"

    text = sse_text([{"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]}])
    with pytest.raises(ServerError, match="MALFORMED"):
        await collect(make(replay(text)), req())

    text = sse_text([{"candidates": [{"content": {"parts": [{"text": "半"}]}}]}])
    with pytest.raises(ServerError, match="意外结束"):
        await collect(make(replay(text)), req())

    text = sse_text([{"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}])
    with pytest.raises(RateLimitError):
        await collect(make(replay(text)), req())


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            400,
            '{"error":{"code":400,"message":"API key not valid. Please pass a valid API key.","status":"INVALID_ARGUMENT"}}',
            AuthenticationError,
        ),
        (
            400,
            '{"error":{"code":400,"message":"The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).","status":"INVALID_ARGUMENT"}}',
            ContextOverflowError,
        ),
        (
            503,
            '{"error":{"code":503,"message":"The model is overloaded.","status":"UNAVAILABLE"}}',
            ServerError,
        ),
    ],
)
async def test_http_errors(replay: Any, status: int, body: str, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        await collect(make(replay(sse_response(body, status=status))), req())


async def test_rate_limit_retry_info(replay: Any) -> None:
    body = (
        '[{"error":{"code":429,"message":"Resource exhausted","status":"RESOURCE_EXHAUSTED",'
        '"details":[{"@type":"type.googleapis.com/google.rpc.RetryInfo","retryDelay":"17s"}]}}]'
    )
    with pytest.raises(RateLimitError) as ei:
        await collect(make(replay(sse_response(body, status=429))), req())
    assert ei.value.retry_after == 17.0
