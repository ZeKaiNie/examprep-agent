"""测试工具：脚本化假模型、虚拟时钟、SSE 构造（对下游包公开）。"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from .accumulator import StreamAccumulator
from .config import ProviderConfig
from .errors import LLMError
from .events import StreamEvent
from .registry import ModelInfo
from .types import LLMRequest, StopReason, Usage


@dataclass
class FakeTurn:
    """假模型的一次回复。"""

    text: str = ""
    thinking: str = ""
    # (工具名, 参数) 或 (工具名, 参数, 调用 id)
    tool_calls: Sequence[tuple[str, dict[str, Any]] | tuple[str, dict[str, Any], str]] = ()
    usage: Usage = field(default_factory=lambda: Usage(input_tokens=10, output_tokens=5))
    stop_reason: StopReason | None = None
    chunk_size: int = 0  # >0 时文本按该长度切成多个增量
    gate: asyncio.Event | None = None  # 输出第一个增量后等待该事件（取消测试用）
    error: LLMError | None = None  # 开始前抛出
    error_after_output: LLMError | None = None  # 输出文本后抛出
    raw_tool_arguments: str | None = None  # 覆盖第一个工具调用的参数原文（测试 JSON 解析失败）


ScriptItem = FakeTurn | LLMError | Callable[[LLMRequest], FakeTurn]


class FakeProvider:
    """按脚本依次回复的 provider，实现 :class:`examprep_llm.protocols.Provider` 协议。"""

    protocol = "fake"

    def __init__(
        self,
        script: Iterable[ScriptItem] = (),
        *,
        name: str = "fake",
        default: FakeTurn | None = None,
        config: ProviderConfig | None = None,
    ) -> None:
        self.name = name
        self.config = config or ProviderConfig(name=name, protocol="fake")
        self.script: deque[ScriptItem] = deque(script)
        self.default = default
        self.requests: list[LLMRequest] = []
        self.active_streams = 0
        self.max_active_streams = 0
        self.closed = False
        self._call_seq = 0

    def push(self, *items: ScriptItem) -> None:
        self.script.extend(items)

    def _next(self, request: LLMRequest) -> FakeTurn | LLMError:
        if self.script:
            item = self.script.popleft()
        elif self.default is not None:
            item = self.default
        else:
            raise AssertionError("FakeProvider 脚本已用完")
        if callable(item):
            return item(request)
        return item

    async def stream(self, request: LLMRequest, model: ModelInfo) -> AsyncIterator[StreamEvent]:
        self.requests.append(request.model_copy(deep=True))
        item = self._next(request)
        if isinstance(item, LLMError):
            raise item
        if item.error is not None:
            raise item.error
        self.active_streams += 1
        self.max_active_streams = max(self.max_active_streams, self.active_streams)
        try:
            acc = StreamAccumulator(provider=self.name, model=model.id, protocol=self.protocol)
            yield acc.start()
            await asyncio.sleep(0)
            gated = False

            async def maybe_gate() -> None:
                nonlocal gated
                if item.gate is not None and not gated:
                    gated = True
                    await item.gate.wait()

            if item.thinking:
                ev = acc.thinking(item.thinking)
                if ev is not None:
                    yield ev
                await maybe_gate()
            for piece in _chunks(item.text, item.chunk_size):
                ev = acc.text(piece)
                if ev is not None:
                    yield ev
                await asyncio.sleep(0)
                await maybe_gate()
            if item.error_after_output is not None:
                raise item.error_after_output
            for i, call in enumerate(item.tool_calls):
                name, args = call[0], call[1]
                call_id = call[2] if len(call) == 3 else f"call_{self._call_seq}"
                self._call_seq += 1
                yield acc.tool_start(i, call_id, name)
                raw = item.raw_tool_arguments if (i == 0 and item.raw_tool_arguments is not None) else None
                delta = acc.tool_args(i, raw if raw is not None else json.dumps(args, ensure_ascii=False))
                if delta is not None:
                    yield delta
                end = acc.tool_end(i)
                if end is not None:
                    yield end
                await maybe_gate()
            await maybe_gate()
            acc.usage = item.usage
            yield acc.finish(item.stop_reason or ("tool_use" if item.tool_calls else "stop"))
        finally:
            self.active_streams -= 1

    async def aclose(self) -> None:
        self.closed = True


def _chunks(text: str, size: int) -> list[str]:
    if not text:
        return []
    if size <= 0:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


class FakeClock:
    """虚拟时钟：``sleep`` 立即推进时间，并让出一次事件循环。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)
        await asyncio.sleep(0)


# ---------------------------------------------------------------- SSE 构造


def sse_text(events: Iterable[dict[str, Any] | str | tuple[str, dict[str, Any]]]) -> str:
    """构造 SSE 文本。元素可以是 dict（data JSON）、str（data 原文）或 (event 名, dict)。"""
    out: list[str] = []
    for ev in events:
        if isinstance(ev, tuple):
            name, data = ev
            out.append(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")
        elif isinstance(ev, str):
            out.append(f"data: {ev}\n\n")
        else:
            out.append(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
    return "".join(out)


class ChunkedStream(httpx.AsyncByteStream):
    """按固定字节数切块的响应体；记录是否被关闭（取消测试用）。可选在某块之后挂起。"""

    def __init__(self, data: bytes, *, chunk_size: int = 0, hang_after: int | None = None) -> None:
        self.data = data
        self.chunk_size = chunk_size
        self.hang_after = hang_after
        self.closed = False
        self.chunks_sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = self.chunk_size or len(self.data) or 1
        for i in range(0, len(self.data), size):
            if self.hang_after is not None and self.chunks_sent >= self.hang_after:
                await asyncio.Event().wait()  # 永远挂起，直到被取消
            self.chunks_sent += 1
            yield self.data[i : i + size]
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        self.closed = True


def sse_response(
    body: str | bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    chunk_size: int = 0,
    hang_after: int | None = None,
) -> httpx.Response:
    data = body.encode("utf-8") if isinstance(body, str) else body
    h = {"content-type": "text/event-stream" if status < 400 else "application/json"}
    h.update(headers or {})
    return httpx.Response(
        status, headers=h, stream=ChunkedStream(data, chunk_size=chunk_size, hang_after=hang_after)
    )
