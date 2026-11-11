"""Agent 钩子（R6.4）。继承 :class:`Hooks` 并覆盖需要的方法。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from kaopu_llm import AssistantMessage, LLMRequest, ToolCallBlock, ToolResultMessage

from .tools import Tool, ToolOutput

if TYPE_CHECKING:
    from .loop import Agent

TurnDecision = Literal["end", "continue"] | None


@dataclass
class TurnContext:
    agent: Agent
    run_id: str
    turn: int
    message: AssistantMessage | None = None
    tool_results: list[ToolResultMessage] = field(default_factory=list[ToolResultMessage])


@dataclass
class ToolCallContext:
    agent: Agent
    run_id: str
    turn: int
    call: ToolCallBlock
    tool: Tool[Any] | None
    params: BaseModel | None


@dataclass(frozen=True)
class ToolBlocked:
    reason: str


class Hooks:
    async def prepare_request(self, ctx: TurnContext, request: LLMRequest) -> LLMRequest:
        """发请求前改写请求（上下文引擎在这里组装）。"""
        return request

    async def before_tool(self, ctx: ToolCallContext) -> ToolBlocked | None:
        """返回 ToolBlocked 即拦截该调用，原因作为错误结果回给模型。"""
        return None

    async def after_tool(self, ctx: ToolCallContext, output: ToolOutput) -> ToolOutput:
        """改写工具结果。"""
        return output

    async def finish_turn(self, ctx: TurnContext) -> TurnDecision:
        """一轮结束时决定：``"end"`` 结束、``"continue"`` 再来一轮、None 按默认规则。"""
        return None
