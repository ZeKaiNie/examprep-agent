"""Pydantic 事件 → Server-Sent Events。

每个事件：``event: <type>`` + ``data: <json>``；流结束时发 ``event: end``。前端用 fetch 读流（POST 请求），
不用 EventSource（它只支持 GET）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel
from starlette.responses import StreamingResponse

END = "end"
ERROR = "error"


def frame(event: str, data: str) -> bytes:
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n".encode()


def model_frame(ev: BaseModel) -> bytes:
    kind = getattr(ev, "type", type(ev).__name__)
    return frame(str(kind), ev.model_dump_json())


def error_frame(detail: str) -> bytes:
    return frame(ERROR, json.dumps({"detail": detail}, ensure_ascii=False))


async def frames(events: AsyncIterator[Any]) -> AsyncIterator[bytes]:
    async for ev in events:
        yield (
            model_frame(ev)
            if isinstance(ev, BaseModel)
            else frame("message", json.dumps(ev, ensure_ascii=False))
        )
    yield frame(END, "{}")


def sse_response(body: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        body,
        media_type="text/event-stream; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def parse(text: str) -> list[tuple[str, Any]]:
    """把 SSE 文本解析成 (事件名, 数据)。给测试和命令行工具用。"""
    out: list[tuple[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event = "message"
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            out.append((event, json.loads("\n".join(data))))
    return out
