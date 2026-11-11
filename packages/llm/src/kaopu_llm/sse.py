"""Server-Sent Events 解码（按 WHATWG HTML 规范的事件流解析规则）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str = "message"
    id: str | None = None
    retry: int | None = None


class SSEDecoder:
    """逐行喂入（行尾已去掉），遇到空行时分发事件。"""

    def __init__(self) -> None:
        self._data: list[str] = []
        self._event = ""
        self._id: str | None = None
        self._last_id: str | None = None
        self._retry: int | None = None
        self._first_line = True

    def decode(self, line: str) -> SSEEvent | None:
        if self._first_line:
            self._first_line = False
            line = line.removeprefix("\ufeff")
        line = line.rstrip("\r\n")
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id":
            if "\x00" not in value:
                self._id = value
        elif field == "retry" and value.isdigit():
            self._retry = int(value)
        return None

    def flush(self) -> SSEEvent | None:
        """流结束时调用：处理未以空行结尾的最后一个事件。"""
        return self._dispatch()

    def _dispatch(self) -> SSEEvent | None:
        if self._id is not None:
            self._last_id = self._id
        if not self._data:
            self._event = ""
            self._id = None
            return None
        ev = SSEEvent(
            data="\n".join(self._data),
            event=self._event or "message",
            id=self._last_id,
            retry=self._retry,
        )
        self._data = []
        self._event = ""
        self._id = None
        return ev


def iter_sse_lines(lines: Iterable[str]) -> list[SSEEvent]:
    """同步解析（测试与工具用）。"""
    dec = SSEDecoder()
    out: list[SSEEvent] = []
    for line in lines:
        ev = dec.decode(line)
        if ev is not None:
            out.append(ev)
    last = dec.flush()
    if last is not None:
        out.append(last)
    return out


async def aiter_sse(response: httpx.Response) -> AsyncIterator[SSEEvent]:
    """从 httpx 流式响应中逐个产出 SSE 事件。"""
    dec = SSEDecoder()
    async for line in response.aiter_lines():
        ev = dec.decode(line)
        if ev is not None:
            yield ev
    last = dec.flush()
    if last is not None:
        yield last
