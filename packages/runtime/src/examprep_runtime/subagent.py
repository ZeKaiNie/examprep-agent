"""子 Agent（R9）：独立上下文运行，通过 ``submit_result`` 工具返回 Pydantic 结构体。

主 Agent 只拿到结构化结果，不把子 Agent 的对话塞进自己的上下文。子 Agent 的任何失败（模型错误、超时、
预算耗尽、没有提交结果）都转为 ``ok=False``，不向调用方抛出；只有调用方自身被取消时才传播 CancelledError。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from examprep_llm import AssistantMessage, CancelToken, Message, SystemMessage, TextBlock, Usage, UserMessage

from .budget import Budget
from .events import AgentEnd, EndReason
from .loop import Agent, LLMClient, RequestDefaults
from .tools import Tool, ToolContext, ToolOutput

T = TypeVar("T", bound=BaseModel)

SUBMIT_TOOL = "submit_result"
NUDGE_TEXT = f"请调用 {SUBMIT_TOOL} 工具提交最终结果，不要只用文字回答。"


@dataclass
class SubAgentResult(Generic[T]):
    ok: bool
    value: T | None = None
    error: str | None = None
    reason: EndReason | None = None
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    messages: list[Message] = field(default_factory=list[Message])


class _SubmitTool(Tool[T]):
    sequential = True

    def __init__(self, result_type: type[T]) -> None:
        self.name = SUBMIT_TOOL
        self.description = "提交最终结果。参数即结果本身；提交后任务结束。"
        self.params = result_type
        self.value: T | None = None

    async def run(self, params: T, ctx: ToolContext) -> ToolOutput:
        self.value = params
        return ToolOutput(content="已提交", terminate=True)


async def run_subagent(
    llm: LLMClient,
    result_type: type[T],
    *,
    instructions: str,
    input: str | Sequence[Message],
    tools: Sequence[Tool[Any]] = (),
    role: str | None = None,
    model: str | None = None,
    budget: Budget | None = None,
    timeout: float | None = 60.0,
    max_nudges: int = 1,
    cancel: CancelToken | None = None,
    system_prefix: Sequence[SystemMessage] = (),
    request_defaults: RequestDefaults | None = None,
) -> SubAgentResult[T]:
    submit = _SubmitTool(result_type)
    system = [*system_prefix, SystemMessage(content=instructions)]
    agent = Agent(
        llm,
        system=system,
        tools=[*tools, submit],
        role=role,
        model=model,
        budget=budget or Budget(max_turns=6, max_tool_calls=20),
        request_defaults=request_defaults,
    )
    token = CancelToken(cancel)
    timed_out = False

    def on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        token.cancel("timeout")

    handle = asyncio.get_running_loop().call_later(timeout, on_timeout) if timeout is not None else None
    pending: str | Sequence[Message] | None = (
        [UserMessage(content=[TextBlock(text=input)])] if isinstance(input, str) else input
    )
    turns = 0
    end: AgentEnd | None = None
    try:
        for attempt in range(max_nudges + 1):
            async for ev in agent.run(pending, cancel=token):
                if ev.type == "turn_end":
                    turns += 1
                elif isinstance(ev, AgentEnd):
                    end = ev
            if submit.value is not None or end is None or end.reason != "completed" or attempt == max_nudges:
                break
            pending = NUDGE_TEXT  # 自然结束却没提交：提醒一次再跑
    finally:
        if handle is not None:
            handle.cancel()
        token.detach()

    usage = sum((m.usage for m in agent.messages if isinstance(m, AssistantMessage)), Usage())
    common: dict[str, Any] = {"usage": usage, "turns": turns, "messages": list(agent.messages)}
    reason = end.reason if end is not None else None
    if submit.value is not None:
        return SubAgentResult(ok=True, value=submit.value, reason=reason, **common)
    if timed_out:
        error = "timeout"
    elif end is not None and end.reason != "completed":
        error = end.error or end.reason
    else:
        error = f"子 Agent 没有调用 {SUBMIT_TOOL} 提交结果"
    return SubAgentResult(ok=False, error=error, reason=reason, **common)
