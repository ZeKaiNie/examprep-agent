"""统一的流式事件（R2.2）。

三种协议的流式输出都转换成这一套事件。``index`` 是该块在最终 ``AssistantMessage.content`` 中的下标。
错误不作为事件出现，而是以 :class:`kaopu_llm.errors.LLMError` 抛出。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .types import AssistantMessage, ToolCallBlock


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartEvent(_Event):
    type: Literal["start"] = "start"
    provider: str
    model: str


class TextDelta(_Event):
    type: Literal["text_delta"] = "text_delta"
    index: int
    text: str


class ThinkingDelta(_Event):
    type: Literal["thinking_delta"] = "thinking_delta"
    index: int
    text: str


class ToolCallStart(_Event):
    type: Literal["tool_call_start"] = "tool_call_start"
    index: int
    id: str
    name: str


class ToolCallDelta(_Event):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    arguments_delta: str


class ToolCallEnd(_Event):
    type: Literal["tool_call_end"] = "tool_call_end"
    index: int
    call: ToolCallBlock


class DoneEvent(_Event):
    type: Literal["done"] = "done"
    message: AssistantMessage


StreamEvent = Annotated[
    StartEvent | TextDelta | ThinkingDelta | ToolCallStart | ToolCallDelta | ToolCallEnd | DoneEvent,
    Field(discriminator="type"),
]
