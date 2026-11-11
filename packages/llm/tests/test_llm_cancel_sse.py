from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

import pytest

from kaopu_llm import AbortedError, CancelToken, cancellable
from kaopu_llm.clock import SystemClock
from kaopu_llm.sse import SSEDecoder, SSEEvent, iter_sse_lines

# ---------------------------------------------------------------- CancelToken


def test_token_callbacks_and_reason() -> None:
    t = CancelToken()
    hits: list[str] = []
    remove = t.on_cancel(lambda: hits.append("a"))
    t.on_cancel(lambda: hits.append("b"))
    remove()
    t.cancel("user")
    t.cancel("again")  # 幂等
    assert t.cancelled
    assert t.reason == "user"
    assert hits == ["b"]
    # 已取消时注册立即触发
    t.on_cancel(lambda: hits.append("c"))
    assert hits == ["b", "c"]
    with pytest.raises(AbortedError):
        t.raise_if_cancelled()


def test_child_follows_parent() -> None:
    parent = CancelToken()
    child = parent.child()
    detached = parent.child()
    detached.detach()
    parent.cancel("stop")
    assert child.cancelled
    assert child.reason == "stop"
    assert not detached.cancelled
    # 父已取消时创建的子令牌立即取消
    assert CancelToken(parent).cancelled


async def test_token_wait() -> None:
    t = CancelToken()
    waiter = asyncio.create_task(t.wait())
    await asyncio.sleep(0)
    assert not waiter.done()
    t.cancel()
    await asyncio.wait_for(waiter, 1)
    await t.wait()  # 已取消时立即返回


async def _numbers(closed: list[bool], hang_at: int | None = None) -> AsyncIterator[int]:
    try:
        for i in range(5):
            if hang_at is not None and i == hang_at:
                await asyncio.Event().wait()
            yield i
    finally:
        closed.append(True)


async def test_cancellable_passthrough() -> None:
    closed: list[bool] = []
    assert [x async for x in cancellable(_numbers(closed), None)] == list(range(5))
    assert [x async for x in cancellable(_numbers(closed), CancelToken())] == list(range(5))


async def test_cancellable_interrupts_hanging_source() -> None:
    closed: list[bool] = []
    token = CancelToken()
    got: list[int] = []

    async def consume() -> None:
        async for x in cancellable(_numbers(closed, hang_at=2), token):
            got.append(x)

    task = asyncio.create_task(consume())
    for _ in range(10):
        await asyncio.sleep(0)
    token.cancel("用户取消")
    with pytest.raises(AbortedError, match="用户取消"):
        await asyncio.wait_for(task, 1)
    assert got == [0, 1]
    assert closed == [True]


async def test_cancellable_propagates_source_error() -> None:
    async def boom() -> AsyncIterator[int]:
        yield 1
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        async for _ in cancellable(boom(), CancelToken()):
            pass


async def test_cancellable_already_cancelled() -> None:
    t = CancelToken()
    t.cancel()
    with pytest.raises(AbortedError):
        async for _ in cancellable(_numbers([]), t):
            pass


async def test_cancellable_consumer_break_closes_source() -> None:
    closed: list[bool] = []
    gen = cancellable(_numbers(closed, hang_at=3), CancelToken())
    async for x in gen:
        if x == 1:
            break
    await gen.aclose()
    assert closed == [True]


async def test_system_clock() -> None:
    c = SystemClock()
    t0 = c.now()
    await c.sleep(0)
    await c.sleep(-1)
    assert c.now() >= t0


# ---------------------------------------------------------------- SSE


def test_sse_spec_cases() -> None:
    lines = [
        "\ufeff: comment",
        "event: add",
        "data: line1",
        "data:line2",
        "id: 7",
        "retry: 3000",
        "",
        "data",  # 无冒号：字段名 data，值为空
        "",
        "",  # 空事件不分发
        "event: x",
        "",  # 只有 event 没有 data：不分发
        "data: tail",  # 不以空行结尾
    ]
    events = iter_sse_lines(lines)
    assert events == [
        SSEEvent(data="line1\nline2", event="add", id="7", retry=3000),
        SSEEvent(data="", event="message", id="7", retry=3000),
        SSEEvent(data="tail", event="message", id="7", retry=3000),
    ]


def test_sse_crlf_and_unknown_fields() -> None:
    dec = SSEDecoder()
    assert dec.decode("data: a\r\n") is None
    assert dec.decode("foo: bar") is None
    assert dec.decode("id: bad\x00id") is None
    assert dec.decode("retry: soon") is None
    ev = dec.decode("\r\n")
    assert ev == SSEEvent(data="a")
    assert dec.flush() is None


@pytest.mark.parametrize("seed", range(5))
async def test_sse_random_chunking(seed: int) -> None:
    """字节随机切块（含切断多字节 UTF-8 字符）后解析结果不变。"""
    import httpx

    from kaopu_llm.sse import aiter_sse
    from kaopu_llm.testing import sse_response, sse_text

    text = sse_text([{"t": "中文内容" * 5}, "[DONE]", ("ping", {"n": 1})])
    expected = iter_sse_lines(text.split("\n"))
    rng = random.Random(seed)
    resp = sse_response(text, chunk_size=rng.randint(1, 7))
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: resp))
    async with client, client.stream("GET", "http://x") as r:
        got = [ev async for ev in aiter_sse(r)]
    assert got == expected
    assert got[1].data == "[DONE]"
    assert got[2].event == "ping"
