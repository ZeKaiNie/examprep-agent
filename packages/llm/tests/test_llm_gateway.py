from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest

from kaopu_llm import (
    AbortedError,
    CallRecord,
    CancelToken,
    DoneEvent,
    Gateway,
    GatewayConfig,
    LLMRequest,
    ModelInfo,
    ModelRegistry,
    Pricing,
    ProviderConfig,
    StartEvent,
    StreamAccumulator,
    StreamEvent,
    TextDelta,
    UserMessage,
)
from kaopu_llm.errors import (
    AuthenticationError,
    BadRequestError,
    NoModelAvailableError,
    RateLimitError,
    ServerError,
)
from kaopu_llm.gateway import (
    BreakerPolicy,
    CircuitBreaker,
    ProviderLimiter,
    RetryPolicy,
    TokenBucket,
    backoff_delay,
)
from kaopu_llm.testing import FakeClock, FakeProvider, FakeTurn, sse_response, sse_text

REQ = LLMRequest(messages=[UserMessage(content="hi")])  # type: ignore[arg-type]


def gw(
    *providers: FakeProvider,
    roles: dict[str, list[str]] | None = None,
    clock: FakeClock | None = None,
    retry: RetryPolicy | None = None,
    breaker: BreakerPolicy | None = None,
    registry: ModelRegistry | None = None,
) -> Gateway:
    return Gateway(
        {p.name: p for p in providers},
        roles or {"tutor": [f"{p.name}/m" for p in providers]},
        clock=clock or FakeClock(),
        rng=random.Random(0),
        retry=retry,
        breaker=breaker,
        registry=registry,
    )


async def run(g: Gateway, **kw: Any) -> list[StreamEvent]:
    kw.setdefault("role", "tutor")
    return [ev async for ev in g.stream(REQ, **kw)]


# ---------------------------------------------------------------- 基本流程与记账


async def test_stream_and_ledger() -> None:
    fake = FakeProvider([FakeTurn(text="你好", chunk_size=1)])
    reg = ModelRegistry(
        [ModelInfo(id="m", provider="fake", protocol="fake", pricing=Pricing(input=1, output=2))]
    )
    g = gw(fake, registry=reg)
    seen: list[CallRecord] = []
    g.on_call(seen.append)
    events = await run(g)
    assert [e.type for e in events] == ["start", "text_delta", "text_delta", "done"]
    assert len(seen) == 1
    rec = seen[0]
    assert (rec.role, rec.model_ref, rec.outcome, rec.attempts) == ("tutor", "fake/m", "ok", 1)
    assert rec.cost is not None
    assert rec.cost.total == pytest.approx((10 * 1 + 5 * 2) / 1_000_000)
    assert rec.ttft is not None
    assert g.ledger.total().input_tokens == 10
    assert g.ledger.by_role()["tutor"].output_tokens == 5
    assert g.ledger.by_model()["fake/m"].output_tokens == 5
    assert g.ledger.cost()["USD"] == pytest.approx(rec.cost.total)
    assert g.model_info("fake/m").pricing is not None
    await g.aclose()
    assert fake.closed


async def test_complete_and_direct_model() -> None:
    fake = FakeProvider([FakeTurn(text="ok")])
    g = gw(fake)
    msg = await g.complete(REQ, model="fake/other")
    assert msg.text == "ok"
    assert msg.model == "other"


def test_chain_validation() -> None:
    g = gw(FakeProvider())
    with pytest.raises(ValueError, match="role"):
        g.chain()
    with pytest.raises(ValueError, match="没有配置"):
        g.chain(role="grader")
    with pytest.raises(ValueError, match="未配置 provider"):
        g.model_info("nope/m")
    with pytest.raises(ValueError, match="provider/model"):
        g.model_info("bad")


# ---------------------------------------------------------------- 重试


async def test_retry_then_success() -> None:
    clock = FakeClock()
    fake = FakeProvider([ServerError("boom"), FakeTurn(text="ok")])
    g = gw(fake, clock=clock)
    events = await run(g)
    assert isinstance(events[-1], DoneEvent)
    assert len(fake.requests) == 2
    assert len(clock.sleeps) == 1
    assert 0.25 <= clock.sleeps[0] <= 0.5
    ok = [r for r in g.ledger.records if r.outcome == "ok"]
    assert ok[0].attempts == 2
    assert g.ledger.errors == 1


async def test_retry_respects_retry_after() -> None:
    clock = FakeClock()
    fake = FakeProvider([RateLimitError("slow", retry_after=5.0), FakeTurn(text="ok")])
    await run(gw(fake, clock=clock))
    assert clock.sleeps == [5.0]


async def test_long_retry_after_falls_back_immediately() -> None:
    clock = FakeClock()
    a = FakeProvider([RateLimitError("slow", retry_after=600.0)], name="a")
    b = FakeProvider([FakeTurn(text="from b")], name="b")
    g = gw(a, b, clock=clock)
    events = await run(g)
    assert events[-1].message.provider == "b"
    assert clock.sleeps == []
    assert g.ledger.records[-1].fallback_from == ("a/m",)


async def test_no_retry_after_output() -> None:
    fake = FakeProvider(
        [FakeTurn(text="部分", error_after_output=ServerError("断了")), FakeTurn(text="不该用到")]
    )
    g = gw(fake)
    got: list[StreamEvent] = []
    with pytest.raises(ServerError, match="断了"):
        async for ev in g.stream(REQ, role="tutor"):
            got.append(ev)
    assert [e.type for e in got] == ["start", "text_delta"]
    assert len(fake.requests) == 1


class StartThenFail:
    """先发 StartEvent 再出错：此时尚未输出内容，仍可重试。"""

    protocol = "fake"

    def __init__(self) -> None:
        self.name = "sf"
        self.config = ProviderConfig(name="sf", protocol="fake")
        self.calls = 0

    async def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        acc = StreamAccumulator(provider=self.name, model=model.id, protocol="fake")
        yield acc.start()
        if self.calls == 1:
            raise ServerError("早期错误")
        ev = acc.text("ok")
        assert ev is not None
        yield ev
        yield acc.finish("stop")

    async def aclose(self) -> None:
        pass


async def test_start_event_withheld_until_content() -> None:
    p = StartThenFail()
    g = Gateway({"sf": p}, {"tutor": ["sf/m"]}, clock=FakeClock(), rng=random.Random(0))
    events = await run(g)
    assert [e.type for e in events] == ["start", "text_delta", "done"]  # 只有一个 start
    assert p.calls == 2


async def test_bad_request_not_retried_nor_fallback() -> None:
    a = FakeProvider([BadRequestError("bad")], name="a")
    b = FakeProvider([FakeTurn(text="b")], name="b")
    with pytest.raises(BadRequestError):
        await run(gw(a, b))
    assert len(b.requests) == 0


async def test_fallback_after_retries_exhausted() -> None:
    a = FakeProvider([ServerError("1"), ServerError("2"), ServerError("3")], name="a")
    b = FakeProvider([FakeTurn(text="b")], name="b")
    clock = FakeClock()
    g = gw(a, b, clock=clock)
    events = await run(g)
    assert events[-1].message.provider == "b"
    assert len(a.requests) == 3
    assert len(clock.sleeps) == 2


async def test_auth_error_falls_back_without_retry() -> None:
    a = FakeProvider([AuthenticationError("no key")], name="a")
    b = FakeProvider([FakeTurn(text="b")], name="b")
    clock = FakeClock()
    events = await run(gw(a, b, clock=clock))
    assert events[-1].message.provider == "b"
    assert len(a.requests) == 1
    assert clock.sleeps == []


async def test_all_fail_raises_last_error() -> None:
    a = FakeProvider([AuthenticationError("a")], name="a")
    b = FakeProvider([AuthenticationError("b")], name="b")
    with pytest.raises(AuthenticationError, match="b"):
        await run(gw(a, b))


def test_backoff_delay_bounds() -> None:
    p = RetryPolicy(base_delay=1.0, max_delay=4.0, max_retry_after=10)
    rng = random.Random(1)
    for failures, cap in [(1, 1.0), (2, 2.0), (3, 4.0), (6, 4.0)]:
        d = backoff_delay(p, failures, rng)
        assert d is not None
        assert cap / 2 <= d <= cap
    assert backoff_delay(p, 1, rng, retry_after=8.0) == 8.0
    assert backoff_delay(p, 1, rng, retry_after=11.0) is None


# ---------------------------------------------------------------- 熔断


def test_breaker_state_machine() -> None:
    clock = FakeClock()
    b = CircuitBreaker(BreakerPolicy(failure_threshold=2, cooldown=10), clock=clock)
    assert b.allow()
    b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    assert b.state == "open"
    assert not b.allow()
    clock.advance(10)
    assert b.state == "half_open"
    assert b.allow()  # 试探
    assert not b.allow()  # 同时只放一个
    b.record_failure()
    assert b.state == "open"
    clock.advance(10)
    assert b.allow()
    b.release()  # 不计成败，允许下一次试探
    assert b.allow()
    b.record_success()
    assert b.state == "closed"
    assert b.failures == 0


async def test_breaker_skips_open_model_and_recovers() -> None:
    clock = FakeClock()
    a = FakeProvider([ServerError("1"), ServerError("2"), FakeTurn(text="a 恢复")], name="a")
    b = FakeProvider(default=FakeTurn(text="b"), name="b")
    g = gw(
        a,
        b,
        clock=clock,
        retry=RetryPolicy(max_attempts=1),
        breaker=BreakerPolicy(failure_threshold=2, cooldown=30),
    )
    for _ in range(3):
        events = await run(g)
        assert events[-1].message.provider == "b"
    assert len(a.requests) == 2  # 第三次时 a 已熔断，被跳过
    assert g.breaker("a/m").state == "open"
    clock.advance(30)
    events = await run(g)
    assert events[-1].message.provider == "a"
    assert g.breaker("a/m").state == "closed"


async def test_all_open_raises_no_model() -> None:
    a = FakeProvider([ServerError("x")], name="a")
    g = gw(a, retry=RetryPolicy(max_attempts=1), breaker=BreakerPolicy(failure_threshold=1))
    with pytest.raises(ServerError):
        await run(g)
    with pytest.raises(NoModelAvailableError):
        await run(g)


# ---------------------------------------------------------------- 限流


async def test_token_bucket_waits() -> None:
    clock = FakeClock()
    bucket = TokenBucket(60, capacity=1, clock=clock)
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps == [pytest.approx(1.0)]
    assert not bucket.try_acquire()
    clock.advance(1.0)
    assert bucket.tokens == pytest.approx(1.0)
    with pytest.raises(ValueError, match="rpm"):
        TokenBucket(0)


async def test_rpm_limit_through_gateway() -> None:
    clock = FakeClock()
    fake = FakeProvider(
        default=FakeTurn(text="x"), config=ProviderConfig(name="fake", protocol="fake", rpm=2)
    )
    g = gw(fake, clock=clock)
    for _ in range(4):
        await run(g)
    # 容量 2，速率 2/分钟：第 3、4 次各等 30 秒
    assert clock.sleeps == [pytest.approx(30.0), pytest.approx(30.0)]


async def test_concurrency_limit() -> None:
    gate = asyncio.Event()
    fake = FakeProvider(
        [FakeTurn(text="a", gate=gate), FakeTurn(text="b", gate=gate), FakeTurn(text="c", gate=gate)],
        config=ProviderConfig(name="fake", protocol="fake", max_concurrency=2),
    )
    g = gw(fake)
    tasks = [asyncio.create_task(run(g)) for _ in range(3)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert fake.active_streams == 2
    assert g.limiter("fake").in_flight == 2
    gate.set()
    await asyncio.gather(*tasks)
    assert fake.max_active_streams == 2
    assert g.limiter("fake").in_flight == 0


async def test_limiter_context_manager() -> None:
    lim = ProviderLimiter(max_concurrency=1)
    async with lim:
        assert lim.in_flight == 1
    assert lim.in_flight == 0


# ---------------------------------------------------------------- 取消


async def test_cancel_mid_stream() -> None:
    gate = asyncio.Event()
    fake = FakeProvider([FakeTurn(text="第一段", gate=gate)])
    g = gw(fake)
    token = CancelToken()
    got: list[StreamEvent] = []

    async def consume() -> None:
        async for ev in g.stream(REQ, role="tutor", cancel=token):
            got.append(ev)
            if isinstance(ev, TextDelta):
                token.cancel("用户点了停止")

    with pytest.raises(AbortedError, match="停止"):
        await asyncio.wait_for(consume(), 1)
    assert [e.type for e in got] == ["start", "text_delta"]
    assert fake.active_streams == 0
    assert g.ledger.records[-1].outcome == "aborted"
    assert g.limiter("fake").in_flight == 0


async def test_cancel_before_start() -> None:
    token = CancelToken()
    token.cancel()
    with pytest.raises(AbortedError):
        await run(gw(FakeProvider([FakeTurn()])), cancel=token)


async def test_cancel_during_backoff() -> None:
    from kaopu_llm.clock import SystemClock

    fake = FakeProvider([ServerError("x"), FakeTurn(text="ok")])
    g = Gateway(
        {"fake": fake},
        {"tutor": ["fake/m"]},
        clock=SystemClock(),
        retry=RetryPolicy(base_delay=30, max_delay=30),
    )
    token = CancelToken()
    task = asyncio.create_task(run(g, cancel=token))
    await asyncio.sleep(0.05)
    token.cancel("stop")
    with pytest.raises(AbortedError):
        await asyncio.wait_for(task, 1)
    assert len(fake.requests) == 1


async def test_cancel_closes_http_stream() -> None:
    """真实协议适配器：取消后 HTTP 响应流被关闭（R5.5）。"""
    import httpx

    from kaopu_llm.config import provider_config
    from kaopu_llm.protocols import OpenAICompatProvider

    body = sse_text(
        [
            {"choices": [{"index": 0, "delta": {"content": "第一句"}}]},
            {"choices": [{"index": 0, "delta": {"content": "第二句"}, "finish_reason": "stop"}]},
            "[DONE]",
        ]
    )
    resp = sse_response(body, chunk_size=len(body.encode()) // 2, hang_after=1)
    stream = resp.stream
    provider = OpenAICompatProvider(provider_config("ollama"), transport=httpx.MockTransport(lambda r: resp))
    g = Gateway({"ollama": provider}, {"tutor": ["ollama/qwen3"]})
    token = CancelToken()

    async def consume() -> list[str]:
        out: list[str] = []
        async for ev in g.stream(REQ, role="tutor", cancel=token):
            out.append(ev.type)
            if isinstance(ev, StartEvent):
                asyncio.get_running_loop().call_later(0.02, token.cancel)
        return out

    with pytest.raises(AbortedError):
        await asyncio.wait_for(consume(), 2)
    assert stream.closed is True
    await g.aclose()


# ---------------------------------------------------------------- 配置


def test_from_config_merges_presets() -> None:
    cfg = GatewayConfig.model_validate(
        {
            "providers": {
                "deepseek": {"api_key": "sk-1", "rpm": 100},
                "myvllm": {"protocol": "openai", "base_url": "http://gpu:8000/v1"},
            },
            "models": [{"id": "qwen", "provider": "myvllm", "protocol": "openai", "context_window": 32768}],
            "roles": {"tutor": ["deepseek/deepseek-flash", "myvllm/qwen"]},
            "retry": {"max_attempts": 2},
        }
    )
    g = Gateway.from_config(cfg)
    ds = g.providers["deepseek"].config
    assert ds.base_url == "https://api.deepseek.com/v1"
    assert ds.resolve_api_key() == "sk-1"
    assert ds.rpm == 100
    assert g.providers["myvllm"].config.base_url == "http://gpu:8000/v1"
    assert g.model_info("myvllm/qwen").context_window == 32768
    assert g.retry.max_attempts == 2
    assert g.chain(role="tutor") == ["deepseek/deepseek-flash", "myvllm/qwen"]
