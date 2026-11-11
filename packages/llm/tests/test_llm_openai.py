from __future__ import annotations

from typing import Any

import httpx
import pytest

from examprep_llm import (
    AssistantMessage,
    DoneEvent,
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
from examprep_llm.errors import (
    ContextOverflowError,
    LLMTimeoutError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from examprep_llm.protocols import OpenAICompatProvider
from examprep_llm.testing import sse_response, sse_text

REG = ModelRegistry()
WEATHER = ToolSpec(
    name="get_weather",
    description="查天气",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


def make(rec: Any, name: str = "deepseek", **opts: Any) -> OpenAICompatProvider:
    cfg = provider_config(name, api_key="sk-test", options=opts or None)
    return OpenAICompatProvider(cfg, transport=rec.transport)


def req(*msgs: Any, **kw: Any) -> LLMRequest:
    return LLMRequest(messages=list(msgs) or [UserMessage(content="北京天气？")], **kw)  # type: ignore[arg-type]


async def collect(p: OpenAICompatProvider, r: LLMRequest, model: str = "deepseek-flash") -> list[Any]:
    info = REG.get(p.name, model, "openai")
    return [ev async for ev in p.stream(r, info)]


async def test_tool_call_stream(replay: Any) -> None:
    rec = replay("openai_tool_call.sse")
    p = make(rec)
    events = await collect(p, req(tools=[WEATHER]))
    assert [e.type for e in events] == [
        "start",
        "tool_call_start",
        "tool_call_delta",
        "tool_call_delta",
        "tool_call_end",
        "done",
    ]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    msg = done.message
    assert msg.stop_reason == "tool_use"
    assert msg.tool_calls == [
        ToolCallBlock(id="call_0_weather", name="get_weather", arguments={"city": "北京"})
    ]
    assert (msg.usage.input_tokens, msg.usage.cache_read_tokens, msg.usage.output_tokens) == (36, 64, 20)
    assert (msg.provider, msg.model, msg.protocol) == ("deepseek", "deepseek-flash", "openai")

    request = rec.requests[0]
    assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = rec.body()
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "get_weather", "description": "查天气", "parameters": WEATHER.parameters},
        }
    ]
    assert body["messages"] == [{"role": "user", "content": "北京天气？"}]
    await p.aclose()


async def test_real_deepseek_flash_recording(replay: Any) -> None:
    """2026-11 录制的 deepseek-flash 真实流：思考模式 + 工具调用 + 缓存用量。"""
    rec = replay("deepseek_flash_tool_call.sse", chunk_size=13)
    events = await collect(make(rec), req(tools=[WEATHER]), model="deepseek-flash")
    msg = events[-1].message
    assert msg.stop_reason == "tool_use"
    assert msg.thinking  # reasoning_content 被识别为推理
    assert msg.tool_calls[0].name == "get_weather"
    assert msg.tool_calls[0].arguments == {"city": "北京"}
    assert msg.tool_calls[0].id.startswith("call_")
    assert msg.usage.output_tokens > 0
    assert msg.usage.reasoning_tokens > 0

    rec = replay("deepseek_flash_text.sse", chunk_size=13)
    msg = (await collect(make(rec), req(), model="deepseek-flash"))[-1].message
    assert msg.stop_reason == "stop"
    assert "北京" in msg.text
    assert msg.usage.cache_read_tokens > 0


async def test_text_stream_with_random_chunks(replay: Any) -> None:
    for size in (1, 3, 17):
        rec = replay("openai_text.sse", chunk_size=size)
        events = await collect(make(rec), req())
        msg = events[-1].message
        assert msg.text == "北京今天晴，25 度。"
        assert msg.stop_reason == "stop"
        assert msg.usage.cache_read_tokens == 128
        assert [e.type for e in events].count("text_delta") == 2


async def test_reasoning_content(replay: Any) -> None:
    rec = replay("openai_reasoning.sse")
    events = await collect(make(rec), req(reasoning="high"), model="deepseek-flash")
    msg = events[-1].message
    assert msg.thinking == "用户问 1+1，答案是 2。"
    assert msg.text == "等于 2。"
    assert isinstance(msg.content[0], ThinkingBlock)
    assert msg.usage.reasoning_tokens == 24
    assert msg.usage.input_tokens == 10
    assert rec.body()["reasoning_effort"] == "high"


async def test_openrouter_reasoning_field(replay: Any) -> None:
    text = sse_text(
        [
            {"choices": [{"index": 0, "delta": {"reasoning": "想想"}}]},
            {"choices": [{"index": 0, "delta": {"content": "好"}, "finish_reason": "stop"}]},
            "[DONE]",
        ]
    )
    rec = replay(text)
    events = await collect(make(rec, "openrouter"), req(), model="x/y")
    assert events[-1].message.thinking == "想想"
    assert "reasoning_effort" not in rec.body()  # 未知模型不支持推理


async def test_parallel_tool_calls(replay: Any) -> None:
    rec = replay("openai_parallel_tools.sse")
    events = await collect(make(rec, "openai"), req(max_tokens=100), model="gpt-4o-mini")
    msg = events[-1].message
    assert [(c.id, c.arguments) for c in msg.tool_calls] == [("call_A", {"q": "a"}), ("call_B", {"q": "b"})]
    assert rec.body()["max_completion_tokens"] == 100
    assert msg.usage.input_tokens == 80


async def test_same_index_different_ids_and_object_args(replay: Any) -> None:
    text = sse_text(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "f"}}]},
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "b", "function": {"name": "g", "arguments": {"k": 1}}}
                            ]
                        },
                    }
                ]
            },
            {"choices": [{"index": 1, "delta": {"content": "忽略非 0 号 choice"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        ]
    )
    rec = replay(text)
    msg = (await collect(make(rec), req()))[-1].message
    assert [(c.id, c.name, c.arguments) for c in msg.tool_calls] == [("a", "f", {}), ("b", "g", {"k": 1})]
    assert msg.text == ""


async def test_invalid_tool_json_kept_raw(replay: Any) -> None:
    text = sse_text(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "a", "function": {"name": "f", "arguments": "{oops"}}
                            ]
                        },
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        ]
    )
    msg = (await collect(make(replay(text)), req()))[-1].message
    assert msg.tool_calls[0].raw_arguments == "{oops"
    assert msg.tool_calls[0].arguments == {}


def test_convert_full_conversation() -> None:
    p = OpenAICompatProvider(
        provider_config("deepseek"), transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    r = req(
        SystemMessage(content="你是助教"),
        UserMessage(content=[TextBlock(text="看图"), ImageBlock(media_type="image/png", data="QUJD")]),
        AssistantMessage(
            content=[
                ThinkingBlock(text="想"),
                TextBlock(text="我查一下"),
                ToolCallBlock(id="c1", name="get_weather", arguments={"city": "北京"}),
            ],
            protocol="openai",
        ),
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="get_weather",
            content=[TextBlock(text="晴"), ImageBlock(media_type="image/png", data="x")],
        ),
        AssistantMessage(content=[], stop_reason="aborted"),  # 空消息被跳过
        temperature=0.2,
        extra={"top_p": 0.9},
    )
    body = p.build_body(r, REG.get("deepseek", "deepseek-flash", "openai"))
    assert body["messages"] == [
        {"role": "system", "content": "你是助教"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ],
        },
        {
            "role": "assistant",
            "content": "我查一下",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "晴\n[图片已省略]"},
    ]
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.9
    assert "max_tokens" not in body


def test_deepseek_thinking_toggle() -> None:
    info = REG.get("deepseek", "deepseek-flash", "openai")
    ds = OpenAICompatProvider(provider_config("deepseek", api_key="k"))
    off = ds.build_body(req(reasoning="off"), info)
    assert off["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in off
    low = ds.build_body(req(reasoning="low"), info)
    assert (low["thinking"], low["reasoning_effort"]) == ({"type": "enabled"}, "low")
    assert "thinking" not in ds.build_body(req(), info)  # 未设置：厂商默认
    # 其他 OpenAI 兼容厂商不认 thinking 字段，不发送
    gpt = REG.get("openai", "gpt-5-mini", "openai")
    body = OpenAICompatProvider(provider_config("openai", api_key="k")).build_body(req(reasoning="off"), gpt)
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_send_reasoning_and_disable_stream_usage() -> None:
    # 覆盖的选项与预设合并：deepseek 预设的 send_reasoning=True 保留
    cfg = provider_config("deepseek", options={"stream_usage": False, "max_tokens_field": "max_tokens"})
    assert cfg.options["send_reasoning"] is True
    p = OpenAICompatProvider(cfg)
    r = req(
        UserMessage(content="q"),  # type: ignore[arg-type]
        AssistantMessage(
            content=[ThinkingBlock(text="推理"), TextBlock(text="答")], provider="deepseek", protocol="openai"
        ),
        AssistantMessage(
            content=[ThinkingBlock(text="别家推理"), TextBlock(text="答2")],
            provider="qwen",
            protocol="openai",
        ),
        max_tokens=50,
    )
    body = p.build_body(r, REG.get("deepseek", "deepseek-flash", "openai"))
    assert body["messages"][1]["reasoning_content"] == "推理"
    assert "reasoning_content" not in body["messages"][2]  # 只回传同一 provider 的推理
    assert "stream_options" not in body
    assert body["max_tokens"] == 50
    # 其他 provider 默认不回传
    other = OpenAICompatProvider(provider_config("qwen")).build_body(
        r, REG.get("qwen", "qwen-plus", "openai")
    )
    assert all("reasoning_content" not in m for m in other["messages"])


async def test_http_errors(replay: Any) -> None:
    rec = replay(
        sse_response(
            '{"error":{"message":"Rate limit","type":"requests","code":"rate_limit_exceeded"}}',
            status=429,
            headers={"retry-after": "3"},
        )
    )
    with pytest.raises(RateLimitError) as ei:
        await collect(make(rec), req())
    assert ei.value.retry_after == 3.0
    assert ei.value.status == 429

    rec = replay(
        sse_response(
            '{"error":{"message":"This model\'s maximum context length is 128000 tokens","code":"context_length_exceeded"}}',
            status=400,
        )
    )
    with pytest.raises(ContextOverflowError):
        await collect(make(rec), req())


async def test_stream_error_chunk_and_truncated_stream(replay: Any) -> None:
    rec = replay(sse_text([{"error": {"message": "upstream overloaded", "type": "server_error"}}]))
    with pytest.raises(ServerError, match="upstream overloaded"):
        await collect(make(rec), req())

    rec = replay(sse_text([{"choices": [{"index": 0, "delta": {"content": "半"}}]}]))
    with pytest.raises(ServerError, match="意外结束"):
        await collect(make(rec), req())


async def test_done_without_finish_reason_and_no_events(replay: Any) -> None:
    events = await collect(make(replay(sse_text(["", "[DONE]"]))), req())
    assert [e.type for e in events] == ["start", "done"]
    assert events[-1].message.stop_reason == "stop"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("refused"), NetworkError),
        (httpx.ReadTimeout("slow"), LLMTimeoutError),
        (httpx.RemoteProtocolError("bad"), NetworkError),
    ],
)
async def test_transport_errors(exc: Exception, expected: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    p = OpenAICompatProvider(provider_config("ollama"), transport=httpx.MockTransport(handler))
    with pytest.raises(expected):
        await collect(p, req(), model="qwen3:8b")
    # 本地 provider 没有 key 时不发 Authorization
    assert p._headers().get("Authorization") is None  # pyright: ignore[reportPrivateUsage]
