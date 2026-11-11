"""错误映射、能力表、配置、拼装器、token 估算、FakeProvider。"""

from __future__ import annotations

import asyncio
import email.utils

import pytest
from pydantic import ValidationError

from examprep_llm import (
    AssistantMessage,
    ImageBlock,
    LLMRequest,
    ModelInfo,
    ModelRegistry,
    Pricing,
    StreamAccumulator,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    build_provider,
    compute_cost,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    parse_model_ref,
    provider_config,
    truncate_to_tokens,
)
from examprep_llm.accumulator import parse_tool_arguments
from examprep_llm.errors import (
    AuthenticationError,
    BadRequestError,
    ContextOverflowError,
    LLMTimeoutError,
    RateLimitError,
    ServerError,
)
from examprep_llm.protocols import QuotaExceededError, map_http_error, map_stream_error, parse_retry_after
from examprep_llm.protocols._http import extract_error
from examprep_llm.registry import FREE
from examprep_llm.testing import FakeClock, FakeProvider, FakeTurn

# ---------------------------------------------------------------- 错误映射


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, '{"error":{"message":"bad key"}}', AuthenticationError),
        (403, "forbidden", AuthenticationError),
        (402, '{"error":{"message":"pay"}}', QuotaExceededError),
        (
            429,
            '{"error":{"message":"You exceeded your current quota","code":"insufficient_quota"}}',
            QuotaExceededError,
        ),
        (429, '{"error":{"message":"slow"}}', RateLimitError),
        (413, "too big", ContextOverflowError),
        (400, '{"error":{"message":"the request exceeds the available context size"}}', ContextOverflowError),
        (400, '{"error":{"message":"bad param"}}', BadRequestError),
        (422, "", BadRequestError),
        (408, "", LLMTimeoutError),
        (409, "", ServerError),
        (500, "<html>oops</html>", ServerError),
        (529, "", ServerError),
    ],
)
def test_map_http_error(status: int, body: str, expected: type[Exception]) -> None:
    err = map_http_error(status, {}, body, provider="p", model="m")
    assert type(err) is expected
    assert err.status == status


def test_extract_error_formats() -> None:
    assert extract_error('{"error":{"message":"m","type":"t","code":"c"}}') == ("m", "t c")
    assert extract_error('[{"error":{"message":"g","status":"UNAVAILABLE"}}]') == ("g", "UNAVAILABLE")
    assert extract_error("plain text") == ("plain text", "")
    assert extract_error("") == ("（无响应体）", "")


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after({"retry-after-ms": "1500"}) == 1.5
    assert parse_retry_after({"retry-after-ms": "x", "retry-after": "2"}) == 2.0
    date = email.utils.formatdate(1000.0 + 30, usegmt=True)
    assert parse_retry_after({"retry-after": date}, now=1000.0) == pytest.approx(30.0)
    assert parse_retry_after({"retry-after": "not a date"}) is None
    assert parse_retry_after({}, '{"error":{"details":[{"retryDelay":"2.5s"}]}}') == 2.5
    assert parse_retry_after({}, '{"error":{"details":[{"retryDelay":"soon"}]}}') is None
    assert parse_retry_after({}) is None


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        ({"type": "rate_limit_error", "message": "x"}, RateLimitError),
        ({"type": "authentication_error", "message": "x"}, AuthenticationError),
        ({"type": "invalid_request_error", "message": "prompt is too long"}, ContextOverflowError),
        ({"type": "invalid_request_error", "message": "bad"}, BadRequestError),
        ({"type": "overloaded_error"}, ServerError),
        ({"status": "RESOURCE_EXHAUSTED", "message": "q"}, RateLimitError),
    ],
)
def test_map_stream_error(err: dict[str, str], expected: type[Exception]) -> None:
    assert type(map_stream_error(err, provider=None, model=None)) is expected


# ---------------------------------------------------------------- 能力表与成本


def test_registry_lookup_order() -> None:
    reg = ModelRegistry()
    builtin = reg.get("deepseek", "deepseek-flash", "openai")
    assert builtin.supports_prompt_cache
    assert builtin.pricing is not None
    unknown = reg.get("deepseek", "new-model", "openai")
    assert (unknown.context_window, unknown.max_output, unknown.pricing) == (8192, 2048, None)
    local = reg.get("ollama", "qwen3:8b", "openai")
    assert local.pricing == FREE
    override = ModelInfo(id="deepseek-flash", provider="deepseek", protocol="openai", context_window=64000)
    reg.register(override)
    assert reg.get("deepseek", "deepseek-flash", "openai").context_window == 64000
    assert override in reg.known()
    assert ModelRegistry(include_builtin=False).known() == []
    assert override.ref == "deepseek/deepseek-flash"


def test_compute_cost() -> None:
    u = Usage(
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=2_000_000,
        cache_write_tokens=1_000_000,
    )
    c = compute_cost(u, Pricing(input=1.0, output=4.0, cache_read=0.1, cache_write=1.25, currency="CNY"))
    assert c is not None
    assert (c.input, c.output, c.cache_read, c.cache_write) == (1.0, 2.0, 0.2, 1.25)
    assert c.total == pytest.approx(4.45)
    assert c.currency == "CNY"
    # 缓存价格缺省按输入价格
    c2 = compute_cost(u, Pricing(input=1.0, output=1.0))
    assert c2 is not None
    assert c2.cache_read == 2.0
    assert compute_cost(u, None) is None


def test_parse_model_ref() -> None:
    assert parse_model_ref("openrouter/anthropic/claude") == ("openrouter", "anthropic/claude")
    for bad in ("nomodel", "/x", "x/"):
        with pytest.raises(ValueError, match="provider/model"):
            parse_model_ref(bad)


def test_provider_config_and_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-key")
    cfg = provider_config("deepseek")
    assert cfg.resolve_api_key() == "env-key"
    assert "env-key" not in repr(cfg)
    assert provider_config("deepseek", api_key="explicit").resolve_api_key() == "explicit"
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert provider_config("deepseek").resolve_api_key() is None
    custom = provider_config("mine", protocol="anthropic", base_url="http://proxy")
    assert custom.protocol == "anthropic"
    assert type(build_provider(custom)).__name__ == "AnthropicProvider"
    assert type(build_provider(provider_config("gemini"))).__name__ == "GeminiProvider"
    with pytest.raises(ValueError, match="fake"):
        build_provider(provider_config("f", protocol="fake"))
    with pytest.raises(ValidationError):
        provider_config("no-protocol")


# ---------------------------------------------------------------- 拼装器


def test_accumulator_blocks_and_snapshot() -> None:
    acc = StreamAccumulator(provider="p", model="m", protocol="x")
    assert acc.text("") is None
    assert acc.thinking("") is None
    acc.set_signature("ignored")  # 无块时忽略
    acc.thinking("t1")
    acc.thinking("", signature="s1")
    acc.text("a")
    acc.text("b")
    acc.close_block()
    acc.text("c")
    acc.set_signature("ts")
    acc.tool_start("k", "id1", "f")
    acc.tool_args("k", '{"x":')
    assert acc.tool_args("missing", "1") is None
    assert acc.tool_end("missing") is None
    partial = acc.snapshot(stop_reason="aborted", include_partial_tools=False)
    assert [b.type for b in partial.content] == ["thinking", "text", "text"]
    acc.tool_args("k", " 1}")
    done = acc.finish("stop")
    msg = done.message
    assert msg.stop_reason == "tool_use"
    assert msg.content == [
        ThinkingBlock(text="t1", signature="s1"),
        TextBlock(text="ab"),
        TextBlock(text="c", signature="ts"),
        ToolCallBlock(id="id1", name="f", arguments={"x": 1}),
    ]


def test_parse_tool_arguments() -> None:
    assert parse_tool_arguments("") == ({}, None)
    assert parse_tool_arguments('{"a": 1}') == ({"a": 1}, None)
    assert parse_tool_arguments("[1]") == ({}, "[1]")
    assert parse_tool_arguments("{") == ({}, "{")


# ---------------------------------------------------------------- token 估算


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文四字") == 4
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("条件概率 P(A|B)") == 4 + 2  # 4 个汉字 + 7 个其他字符


def test_estimate_message_tokens() -> None:
    assert estimate_message_tokens(SystemMessage(content="中文")) == 6
    user = UserMessage(content=[TextBlock(text="中"), ImageBlock(media_type="image/png", data="x")])
    assert estimate_message_tokens(user) == 4 + 1 + 1000
    a = AssistantMessage(content=[TextBlock(text="中"), ToolCallBlock(id="1", name="abcd", arguments={})])
    assert estimate_message_tokens(a) == 4 + 1 + 1 + 1
    tr = ToolResultMessage(tool_call_id="1", tool_name="f", content=[TextBlock(text="中")])
    assert estimate_messages_tokens([user, tr]) == 1005 + 5


def test_truncate_to_tokens() -> None:
    text = "中" * 100
    assert truncate_to_tokens(text, 200) == text
    cut = truncate_to_tokens(text, 20)
    assert cut.endswith("…（已截断）")
    assert estimate_tokens(cut) <= 20
    assert truncate_to_tokens(text, 3) == ""


# ---------------------------------------------------------------- FakeProvider


async def test_fake_provider_features() -> None:
    model = ModelInfo(id="m", provider="fake", protocol="fake")
    fake = FakeProvider(
        [
            FakeTurn(
                text="x", thinking="想", tool_calls=[("f", {"a": 1}, "my-id")], raw_tool_arguments="{bad"
            ),
            lambda req: FakeTurn(text=f"收到 {len(req.messages)} 条"),
            FakeTurn(error=ServerError("e")),
        ]
    )
    req = LLMRequest(messages=[UserMessage(content="q")])  # type: ignore[arg-type]
    events = [e async for e in fake.stream(req, model)]
    msg = events[-1].message
    assert msg.thinking == "想"
    assert msg.tool_calls[0].id == "my-id"
    assert msg.tool_calls[0].raw_arguments == "{bad"
    events = [e async for e in fake.stream(req, model)]
    assert events[-1].message.text == "收到 1 条"
    with pytest.raises(ServerError):
        _ = [e async for e in fake.stream(req, model)]
    with pytest.raises(AssertionError, match="用完"):
        _ = [e async for e in fake.stream(req, model)]
    fake.push(FakeTurn(text="later"))
    assert [e async for e in fake.stream(req, model)][-1].message.text == "later"
    assert len(fake.requests) == 5
    fake.responder = lambda r: FakeTurn(text=f"动态 {len(r.messages)}")
    assert [e async for e in fake.stream(req, model)][-1].message.text == "动态 1"
    fake.responder = lambda r: ServerError("down")
    with pytest.raises(ServerError):
        _ = [e async for e in fake.stream(req, model)]


async def test_fake_clock() -> None:
    clock = FakeClock(start=5)
    await clock.sleep(2)
    await clock.sleep(-1)
    clock.advance(1)
    assert clock.now() == 8
    assert clock.sleeps == [2, -1]
    await asyncio.sleep(0)
