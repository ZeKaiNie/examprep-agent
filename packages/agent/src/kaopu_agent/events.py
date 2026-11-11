"""Agent 事件（R6.1）。前端、trace、会话记录都订阅同一个事件流。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kaopu_llm import AssistantMessage, Message, StreamEvent, ToolCallBlock, ToolResultMessage

EndReason = Literal["completed", "aborted", "error", "max_turns", "max_tool_calls", "loop_detected"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentStart(_Event):
    type: Literal["agent_start"] = "agent_start"
    run_id: str


class AgentEnd(_Event):
    type: Literal["agent_end"] = "agent_end"
    run_id: str
    reason: EndReason
    error: str | None = None
    new_messages: list[Message] = Field(default_factory=list[Message])


class TurnStart(_Event):
    type: Literal["turn_start"] = "turn_start"
    turn: int


class TurnEnd(_Event):
    type: Literal["turn_end"] = "turn_end"
    turn: int
    message: AssistantMessage | None = None
    tool_results: list[ToolResultMessage] = Field(default_factory=list[ToolResultMessage])


MessageRole = Literal["system", "user", "assistant", "tool_result"]


class MessageStart(_Event):
    type: Literal["message_start"] = "message_start"
    role: MessageRole
    # 助手消息流式开始时为 None
    message: Message | None = None


class MessageUpdate(_Event):
    type: Literal["message_update"] = "message_update"
    event: StreamEvent


class MessageEnd(_Event):
    type: Literal["message_end"] = "message_end"
    message: Message


class ToolStart(_Event):
    type: Literal["tool_start"] = "tool_start"
    call: ToolCallBlock


class ToolUpdate(_Event):
    type: Literal["tool_update"] = "tool_update"
    call_id: str
    tool_name: str
    data: Any = None


class ToolEnd(_Event):
    type: Literal["tool_end"] = "tool_end"
    call_id: str
    tool_name: str
    result: ToolResultMessage
    details: Any = None


AgentEvent = Annotated[
    AgentStart
    | AgentEnd
    | TurnStart
    | TurnEnd
    | MessageStart
    | MessageUpdate
    | MessageEnd
    | ToolStart
    | ToolUpdate
    | ToolEnd,
    Field(discriminator="type"),
]
