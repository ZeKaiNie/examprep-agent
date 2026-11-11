"""由事件投影出当前状态（R7.2）。"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import TypeAdapter

from kaopu_llm import Message

from .store import StoredEvent

MESSAGE_EVENT = "message"
RUN_END_EVENT = "run_end"

_MESSAGE = TypeAdapter[Message](Message)


def project_messages(events: Iterable[StoredEvent]) -> list[Message]:
    """按 seq 顺序取出全部 ``message`` 事件还原对话。其他类型（run_end 等）不影响对话。"""
    return [_MESSAGE.validate_python(e.payload) for e in events if e.type == MESSAGE_EVENT]


def message_payload(message: Message) -> dict[str, object]:
    return _MESSAGE.dump_python(message, mode="json")
