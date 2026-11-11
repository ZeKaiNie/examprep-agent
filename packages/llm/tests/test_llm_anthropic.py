from __future__ import annotations

from typing import Any

import pytest

from kaopu_llm import (
    AssistantMessage,
    ImageBlock,
    LLMRequest,
    ModelInfo,
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
from kaopu_llm.errors import AuthenticationError, ContextOverflowError, RateLimitError, ServerError
from kaopu_llm.protocols import AnthropicProvider
from kaopu_llm.testing import sse_response, sse_text

REG = ModelRegistry()
SONNET = REG.get("anthropic", "claude-sonnet-4-5", "anthropic")
TOOL = ToolSpec(name="get_weather", description="查天气", parameters={"type": "object", "properties": {}})


def make(rec: Any, **opts: Any) -> AnthropicProvider:
    return AnthropicProvider(
        provider_config("anthropic", api_key="ak", options=opts or None), transport=rec.transport
    )


async def collect(p: AnthropicProvider, r: LLMRequest, model: ModelInfo = SONNET) -> list[Any]:
    return [ev async for ev in p.stream(r, model)]


def req(*msgs: Any, **kw: Any) -> LLMRequest:
    return LLMRequest(messages=list(msgs) or [UserMessage(content="北京天气？")], **kw)  # type: ignore[arg-type]


async def test_tool_call_with_thinking(replay: Any) -> None:
    rec = replay("anthropic_tool_call.sse")
    events = await collect(make(rec), req(tools=[TOOL]))
    types = [e.type for e in events]
    assert types[0] == "start"
    assert types[-1] == "done"
    assert "thinking_delta" in types
    assert types.count("tool_call_delta") == 2  # 空的 partial_json 不产生事件
    msg = events[-1].message
    assert msg.stop_reason == "tool_use"
    assert msg.content == [
        ThinkingBlock(text="需要先查天气。", signature="EqQBCgIYAhIM"),
        TextBlock(text="我来查一下。"),
        ToolCallBlock(id="toolu_01", name="get_weather", arguments={"city": "北京"}),
    ]
    u = msg.usage
    assert (u.input_tokens, u.cache_write_tokens, u.cache_read_tokens, u.output_tokens) == (50, 200, 1000, 89)

    request = rec.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "ak"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = rec.body()
    assert body["max_tokens"] == SONNET.max_output
    assert body["tools"] == [
        {"name": "get_weather", "description": "查天气", "input_schema": TOOL.parameters}
    ]


async def test_text_stream_random_chunks(replay: Any) -> None:
    for size in (1, 5, 64):
        rec = replay("anthropic_text.sse", chunk_size=size)
        msg = (await collect(make(rec), req()))[-1].message
        assert msg.text == "北京今天晴，25 度。"
        assert msg.stop_reason == "stop"
        assert msg.usage.cache_read_tokens == 1200
        assert msg.usage.output_tokens == 15


async def test_overloaded_error_event(replay: Any) -> None:
    with pytest.raises(ServerError, match="Overloaded"):
        await collect(make(replay("anthropic_overloaded.sse")), req())


async def test_stream_without_message_stop(replay: Any) -> None:
    text = sse_text([("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}})])
    with pytest.raises(ServerError, match="message_stop"):
        await collect(make(replay(text)), req())


async def test_redacted_thinking_and_refusal(replay: Any) -> None:
    text = sse_text(
        [
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "redacted_thinking", "data": "ENC"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": "不行"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "refusal"}, "usage": {}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    msg = (await collect(make(replay(text)), req()))[-1].message
    assert msg.content == [ThinkingBlock(text="ENC", redacted=True), TextBlock(text="不行")]
    assert msg.stop_reason == "content_filter"


def test_convert_messages_merges_and_caches() -> None:
    p = AnthropicProvider(provider_config("anthropic", options={"beta": "x"}))
    r = req(
        SystemMessage(content="L0 策略", cacheable=True),
        SystemMessage(content="L4 本轮"),
        UserMessage(content=[TextBlock(text="看"), ImageBlock(media_type="image/png", data="AA")]),
        AssistantMessage(
            content=[
                ThinkingBlock(text="想", signature="sig"),
                ThinkingBlock(text="无签名的丢弃"),
                ThinkingBlock(text="ENC", redacted=True),
                TextBlock(text=""),
                ToolCallBlock(id="t1", name="get_weather", arguments={"city": "北京"}),
                ToolCallBlock(id="t2", name="get_weather", arguments={"city": "上海"}),
            ],
            protocol="anthropic",
        ),
        ToolResultMessage(tool_call_id="t1", tool_name="get_weather", content="晴"),
        ToolResultMessage(tool_call_id="t2", tool_name="get_weather", content=[], is_error=True),
        UserMessage(content="继续"),
        tools=[TOOL],
        temperature=0.5,
    )
    body = p.build_body(r, SONNET)
    assert body["system"] == [
        {"type": "text", "text": "L0 策略", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "L4 本轮"},
    ]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["temperature"] == 0.5
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA"},
    }
    assert msgs[1]["content"] == [
        {"type": "thinking", "thinking": "想", "signature": "sig"},
        {"type": "redacted_thinking", "data": "ENC"},
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {"city": "北京"}},
        {"type": "tool_use", "id": "t2", "name": "get_weather", "input": {"city": "上海"}},
    ]
    # 两个 tool_result 与后续 user 文本合并成一条 user 消息
    assert msgs[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "晴"}]},
        {
            "type": "tool_result",
            "tool_use_id": "t2",
            "content": [{"type": "text", "text": "（空）"}],
            "is_error": True,
        },
        {"type": "text", "text": "继续"},
    ]
    assert p._headers()["anthropic-beta"] == "x"  # pyright: ignore[reportPrivateUsage]


def test_cross_protocol_thinking_dropped_and_thinking_budget() -> None:
    p = AnthropicProvider(provider_config("anthropic"))
    r = req(
        UserMessage(content="q"),
        AssistantMessage(
            content=[ThinkingBlock(text="别家推理", signature="s"), TextBlock(text="答")], protocol="openai"
        ),
        reasoning="medium",
        temperature=0.3,
        max_tokens=20000,
    )
    body = p.build_body(r, SONNET)
    assert body["messages"][1]["content"] == [{"type": "text", "text": "答"}]
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert "temperature" not in body  # thinking 开启时不能设 temperature
    # max_tokens 太小则不开启 thinking
    small = p.build_body(req(reasoning="high", max_tokens=1500), SONNET)
    assert "thinking" not in small
    # 不支持缓存的模型不加 cache_control
    plain = REG.get("anthropic", "unknown-model", "anthropic")
    body2 = p.build_body(
        req(SystemMessage(content="s", cacheable=True), UserMessage(content="q"), tools=[TOOL]), plain
    )
    assert "cache_control" not in body2["system"][0]
    assert "cache_control" not in body2["tools"][0]


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected"),
    [
        (
            401,
            '{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}',
            {},
            AuthenticationError,
        ),
        (
            429,
            '{"type":"error","error":{"type":"rate_limit_error","message":"slow"}}',
            {"retry-after": "7"},
            RateLimitError,
        ),
        (
            400,
            '{"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 210000 tokens > 200000 maximum"}}',
            {},
            ContextOverflowError,
        ),
        (529, '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}', {}, ServerError),
    ],
)
async def test_http_errors(
    replay: Any, status: int, body: str, headers: dict[str, str], expected: type[Exception]
) -> None:
    rec = replay(sse_response(body, status=status, headers=headers))
    with pytest.raises(expected):
        await collect(make(rec), req())
