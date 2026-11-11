"""Agent 循环（R6）。

一次 ``run`` 是一个异步生成器，产出 :mod:`kaopu_agent.events` 中的事件。流程见设计文档 3.6 节。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kaopu_llm import (
    AbortedError,
    AssistantMessage,
    CancelToken,
    DoneEvent,
    LLMError,
    LLMRequest,
    Message,
    ReasoningLevel,
    StartEvent,
    StopReason,
    StreamAccumulator,
    StreamEvent,
    SystemMessage,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolCallBlock,
    ToolResultMessage,
    ToolSpec,
    UserMessage,
)

from .budget import Budget, CallTracker
from .events import (
    AgentEnd,
    AgentEvent,
    AgentStart,
    EndReason,
    MessageEnd,
    MessageRole,
    MessageStart,
    MessageUpdate,
    ToolEnd,
    ToolStart,
    ToolUpdate,
    TurnEnd,
    TurnStart,
)
from .hooks import Hooks, ToolBlocked, ToolCallContext, TurnContext
from .tools import Tool, ToolContext, ToolOutput, format_validation_error

log = logging.getLogger(__name__)

CANCELLED_TEXT = "已取消：工具未执行完成"
SKIPPED_TEXT = "未执行：本批调用已中止。"


class LLMClient(Protocol):
    """Agent 依赖的模型接口；:class:`kaopu_llm.Gateway` 满足该协议。"""

    def stream(
        self,
        request: LLMRequest,
        *,
        role: str | None = None,
        model: str | None = None,
        cancel: CancelToken | None = None,
    ) -> AsyncIterator[StreamEvent]: ...


class RequestDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int | None = None
    temperature: float | None = None
    reasoning: ReasoningLevel | None = None
    extra: dict[str, Any] = Field(default_factory=dict[str, Any])


AgentInput = str | Message | Sequence[Message] | None


def to_user_message(message: str | UserMessage) -> UserMessage:
    return UserMessage(content=[TextBlock(text=message)]) if isinstance(message, str) else message


@dataclass
class _Prepared:
    call: ToolCallBlock
    tool: Tool[Any] | None = None
    params: BaseModel | None = None
    output: ToolOutput | None = None  # 预检阶段就确定的结果（错误、拦截、跳过）
    ended: bool = False


@dataclass
class _BatchOutcome:
    results: list[ToolResultMessage]
    stop: EndReason | None = None
    terminate: bool = False


class Agent:
    def __init__(
        self,
        llm: LLMClient,
        *,
        system: str | Sequence[SystemMessage] | None = None,
        tools: Sequence[Tool[Any]] = (),
        hooks: Hooks | None = None,
        budget: Budget | None = None,
        role: str | None = None,
        model: str | None = None,
        messages: Sequence[Message] = (),
        request_defaults: RequestDefaults | None = None,
    ) -> None:
        if role is None and model is None:
            raise ValueError("Agent 需要 role 或 model")
        self.llm = llm
        if system is None:
            self.system: list[SystemMessage] = []
        elif isinstance(system, str):
            self.system = [SystemMessage(content=system, cacheable=True)]
        else:
            self.system = list(system)
        self.tools: dict[str, Tool[Any]] = {}
        for t in tools:
            if t.name in self.tools:
                raise ValueError(f"工具重名：{t.name}")
            self.tools[t.name] = t
        self._specs: list[ToolSpec] = [t.spec() for t in self.tools.values()]
        self.hooks = hooks or Hooks()
        self.budget = budget or Budget()
        self.role = role
        self.model = model
        self.messages: list[Message] = list(messages)
        self.request_defaults = request_defaults or RequestDefaults()
        self._steer: deque[UserMessage] = deque()
        self._follow: deque[UserMessage] = deque()
        self._token: CancelToken | None = None
        self.running = False

    # ------------------------------------------------------------ 控制

    def steer(self, message: str | UserMessage) -> None:
        """当前工具批次结束后注入（运行中插话）。"""
        self._steer.append(to_user_message(message))

    def follow_up(self, message: str | UserMessage) -> None:
        """本次运行自然结束后注入。"""
        self._follow.append(to_user_message(message))

    def abort(self, reason: str = "user") -> bool:
        if self._token is None:
            return False
        self._token.cancel(reason)
        return True

    def drain_queues(self) -> list[UserMessage]:
        """取走尚未注入的 steer / follow_up 消息（lane 在运行结束后调用，保证不丢）。"""
        out = [*self._steer, *self._follow]
        self._steer.clear()
        self._follow.clear()
        return out

    @property
    def pending_messages(self) -> int:
        return len(self._steer) + len(self._follow)

    def _requeue(self, pending: list[Message]) -> None:
        users = [m for m in pending if isinstance(m, UserMessage)]
        self._steer.extendleft(reversed(users))

    def build_request(self) -> LLMRequest:
        d = self.request_defaults
        return LLMRequest(
            messages=[*self.system, *self.messages],
            tools=list(self._specs),
            max_tokens=d.max_tokens,
            temperature=d.temperature,
            reasoning=d.reasoning,
            extra=dict(d.extra),
        )

    # ------------------------------------------------------------ 运行

    async def run(
        self, input: AgentInput = None, *, cancel: CancelToken | None = None
    ) -> AsyncIterator[AgentEvent]:
        if self.running:
            raise RuntimeError("Agent 正在运行，不能重入")
        self.running = True
        token = CancelToken(cancel)
        self._token = token
        run_id = uuid.uuid4().hex
        start_index = len(self.messages)
        tracker = CallTracker(self.budget)
        reason: EndReason = "completed"
        error: str | None = None
        try:
            yield AgentStart(run_id=run_id)
            pending = _normalize_input(input)
            turn = 0
            while True:
                if token.cancelled or turn >= self.budget.max_turns:
                    reason = "aborted" if token.cancelled else "max_turns"
                    self._requeue(pending)  # 未注入的插话放回队列，不丢消息
                    break
                turn += 1
                yield TurnStart(turn=turn)
                for m in pending:
                    self.messages.append(m)
                    yield MessageStart(role=_role(m), message=m)
                    yield MessageEnd(message=m)
                pending = []
                ctx = TurnContext(agent=self, run_id=run_id, turn=turn)
                request = await self.hooks.prepare_request(ctx, self.build_request())

                # ---- 模型流式输出
                yield MessageStart(role="assistant")
                mirror: StreamAccumulator | None = None
                assistant: AssistantMessage | None = None
                failure: EndReason | None = None
                try:
                    async for ev in self.llm.stream(request, role=self.role, model=self.model, cancel=token):
                        if isinstance(ev, StartEvent):
                            mirror = StreamAccumulator(provider=ev.provider, model=ev.model, protocol="")
                        elif isinstance(ev, DoneEvent):
                            assistant = ev.message
                        elif mirror is not None:
                            if isinstance(ev, TextDelta):
                                mirror.text(ev.text)
                            elif isinstance(ev, ThinkingDelta):
                                mirror.thinking(ev.text)
                        yield MessageUpdate(event=ev)
                    if assistant is None:
                        raise LLMError("模型流没有给出完成事件")
                except AbortedError:
                    failure = "aborted"
                    assistant = _partial(mirror, "aborted", token.reason or "aborted")
                except LLMError as exc:
                    failure = "error"
                    error = str(exc)
                    assistant = _partial(mirror, "error", error)
                self.messages.append(assistant)
                yield MessageEnd(message=assistant)
                ctx.message = assistant
                if failure is not None:
                    reason = failure
                    yield TurnEnd(turn=turn, message=assistant)
                    break

                # ---- 工具
                calls = assistant.tool_calls
                outcome = _BatchOutcome(results=[])
                if calls:
                    async for ev in self._execute_tools(calls, ctx, token, tracker, outcome):
                        yield ev
                    for r in outcome.results:
                        self.messages.append(r)
                        yield MessageStart(role="tool_result", message=r)
                        yield MessageEnd(message=r)
                ctx.tool_results = outcome.results
                if outcome.stop is not None:
                    reason = outcome.stop
                    yield TurnEnd(turn=turn, message=assistant, tool_results=outcome.results)
                    break

                decision = await self.hooks.finish_turn(ctx)
                yield TurnEnd(turn=turn, message=assistant, tool_results=outcome.results)
                if outcome.terminate and decision != "continue":
                    break
                if self._steer:
                    pending = _pop_all(self._steer)
                    continue
                if decision == "end":
                    break
                if calls or decision == "continue":
                    continue
                if self._follow:
                    pending = _pop_all(self._follow)
                    continue
                break
        except Exception as exc:  # 钩子或工具框架的意外错误：结束本次运行，不让生成器崩掉调用方
            log.exception("Agent 运行出错")
            reason = "error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.running = False
            self._token = None
            token.detach()
        yield AgentEnd(run_id=run_id, reason=reason, error=error, new_messages=self.messages[start_index:])

    # ------------------------------------------------------------ 工具执行

    async def _execute_tools(
        self,
        calls: list[ToolCallBlock],
        turn_ctx: TurnContext,
        token: CancelToken,
        tracker: CallTracker,
        outcome: _BatchOutcome,
    ) -> AsyncIterator[AgentEvent]:
        prepared = [_Prepared(call=c) for c in calls]

        # 1. 预检：按模型给出的顺序串行
        for p in prepared:
            yield ToolStart(call=p.call)
            if outcome.stop is not None:
                p.output = ToolOutput.error(SKIPPED_TEXT)
            else:
                verdict = tracker.check(p.call)
                if verdict != "ok":
                    outcome.stop = verdict
                    p.output = ToolOutput.error(_skip_text(verdict))
                else:
                    p.output = await self._preflight(p, turn_ctx)
            if p.output is not None:
                p.ended = True
                yield self._tool_end(p, p.output)

        # 2. 执行：连续的并行工具成组并发，sequential 工具单独成组
        runnable = [p for p in prepared if p.output is None]
        for group in _groups(runnable):
            if token.cancelled:
                break
            async for ev in self._run_group(group, turn_ctx, token):
                yield ev
        for p in runnable:
            if p.output is None:  # 取消后未执行的组
                p.output = ToolOutput.error(CANCELLED_TEXT)
                p.ended = True
                yield self._tool_end(p, p.output)
        if token.cancelled and outcome.stop is None:
            outcome.stop = "aborted"

        # 3. 按模型顺序写回
        for p in prepared:
            assert p.output is not None
            outcome.results.append(
                ToolResultMessage(
                    tool_call_id=p.call.id,
                    tool_name=p.call.name,
                    content=p.output.blocks(),
                    is_error=p.output.is_error,
                )
            )
        outcome.terminate = all(p.output is not None and p.output.terminate for p in prepared)

    async def _preflight(self, p: _Prepared, turn_ctx: TurnContext) -> ToolOutput | None:
        call = p.call
        tool = self.tools.get(call.name)
        if tool is None:
            names = "、".join(self.tools) or "（无）"
            return ToolOutput.error(f"未知工具 {call.name!r}。可用工具：{names}")
        p.tool = tool
        if call.raw_arguments is not None:
            return ToolOutput.error(f"参数不是合法的 JSON 对象：{call.raw_arguments[:500]}")
        try:
            p.params = tool.validate(call.arguments)
        except ValidationError as exc:
            return ToolOutput.error(format_validation_error(exc))
        blocked = await self.hooks.before_tool(self._call_ctx(p, turn_ctx))
        if isinstance(blocked, ToolBlocked):
            return ToolOutput.error(f"调用被拦截：{blocked.reason}")
        return None

    async def _run_group(
        self, group: list[_Prepared], turn_ctx: TurnContext, token: CancelToken
    ) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

        def finish(p: _Prepared, output: ToolOutput) -> None:
            if p.ended:
                return
            p.output = output
            p.ended = True
            queue.put_nowait(self._tool_end(p, output))

        async def run_one(p: _Prepared) -> None:
            assert p.tool is not None
            assert p.params is not None
            tool = p.tool
            ctx = ToolContext(
                p.call,
                token.child(),
                lambda data: queue.put_nowait(
                    ToolUpdate(call_id=p.call.id, tool_name=p.call.name, data=data)
                ),
                agent=self,
            )
            output: ToolOutput
            try:
                try:
                    if tool.timeout is not None:
                        async with asyncio.timeout(tool.timeout):
                            raw = await tool.run(p.params, ctx)
                    else:
                        raw = await tool.run(p.params, ctx)
                    output = raw if isinstance(raw, ToolOutput) else ToolOutput(content=raw)
                except asyncio.CancelledError:
                    if not token.cancelled:
                        raise
                    output = ToolOutput.error(CANCELLED_TEXT)
                except TimeoutError:
                    output = ToolOutput.error(f"工具执行超时（{tool.timeout} 秒）")
                except Exception as exc:
                    output = ToolOutput.error(f"工具出错：{type(exc).__name__}: {exc}")
                if not token.cancelled:
                    output = await self.hooks.after_tool(self._call_ctx(p, turn_ctx), output)
            finally:
                ctx.cancel.detach()
            finish(p, output)

        def on_done(p: _Prepared) -> Callable[[asyncio.Task[None]], None]:
            # 任务在开始前就被取消时 run_one 不会执行，这里兜底给出结果
            def callback(task: asyncio.Task[None]) -> None:
                if not p.ended:
                    finish(p, ToolOutput.error(_task_error(task)))

            return callback

        tasks: list[asyncio.Task[None]] = []
        for p in group:
            task = asyncio.create_task(run_one(p))
            task.add_done_callback(on_done(p))
            tasks.append(task)

        def cancel_all() -> None:
            for t in tasks:
                t.cancel()

        remove = token.on_cancel(cancel_all)
        try:
            remaining = len(group)
            while remaining:
                ev = await queue.get()
                if isinstance(ev, ToolEnd):
                    remaining -= 1
                yield ev
            while not queue.empty():  # 结束后残留的进度事件
                yield queue.get_nowait()
        finally:
            remove()
            for t in tasks:
                if not t.done():
                    t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*tasks, return_exceptions=True)

    def _call_ctx(self, p: _Prepared, turn_ctx: TurnContext) -> ToolCallContext:
        return ToolCallContext(
            agent=self, run_id=turn_ctx.run_id, turn=turn_ctx.turn, call=p.call, tool=p.tool, params=p.params
        )

    @staticmethod
    def _tool_end(p: _Prepared, output: ToolOutput) -> ToolEnd:
        result = ToolResultMessage(
            tool_call_id=p.call.id, tool_name=p.call.name, content=output.blocks(), is_error=output.is_error
        )
        return ToolEnd(call_id=p.call.id, tool_name=p.call.name, result=result, details=output.details)


# ---------------------------------------------------------------- 辅助


def _normalize_input(value: AgentInput) -> list[Message]:
    if value is None:
        return []
    if isinstance(value, str):
        return [to_user_message(value)]
    if isinstance(value, SystemMessage | UserMessage | AssistantMessage | ToolResultMessage):
        return [value]
    return list(value)


def _role(m: Message) -> MessageRole:
    return m.role


def _pop_all(q: deque[UserMessage]) -> list[Message]:
    out: list[Message] = list(q)
    q.clear()
    return out


def _partial(mirror: StreamAccumulator | None, stop: StopReason, error: str) -> AssistantMessage:
    """取消或出错时的部分消息：保留已输出的文本，丢弃未完成的工具调用（否则下次请求缺少工具结果）。"""
    msg = (
        mirror.snapshot(stop_reason=stop, include_partial_tools=False)
        if mirror
        else AssistantMessage(stop_reason=stop)
    )
    msg.error = error
    return msg


def _groups(items: list[_Prepared]) -> list[list[_Prepared]]:
    groups: list[list[_Prepared]] = []
    current: list[_Prepared] = []
    for p in items:
        assert p.tool is not None
        if p.tool.sequential:
            if current:
                groups.append(current)
                current = []
            groups.append([p])
        else:
            current.append(p)
    if current:
        groups.append(current)
    return groups


def _skip_text(reason: Literal["loop_detected", "max_tool_calls"]) -> str:
    if reason == "loop_detected":
        return "检测到相同工具与参数的重复调用，已停止执行。请换一种做法或直接回答。"
    return "本次运行的工具调用次数已达上限，未执行。"


def _task_error(task: asyncio.Task[None]) -> str:
    if task.cancelled():
        return CANCELLED_TEXT
    exc = task.exception()
    return f"工具框架出错：{type(exc).__name__}: {exc}" if exc else CANCELLED_TEXT
