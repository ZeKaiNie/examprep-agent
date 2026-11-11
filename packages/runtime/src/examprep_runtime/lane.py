"""会话 lane（R8）：同一会话的请求串行执行，不同会话并行。

每个会话一个 lane：一个待运行队列 + 一个消费任务。运行中到达的新消息按策略处理：

- ``queue``：当前运行结束后单独运行（默认）
- ``steer``：注入当前运行，在本轮工具批次后生效
- ``follow_up``：注入当前运行，在其自然结束后生效
- ``collect``：缓冲；当前运行结束后（空闲时则等待 ``collect_window`` 秒）合并为一条消息运行
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from examprep_llm import SYSTEM_CLOCK, Clock, ImageBlock, Message, TextBlock, UserMessage

from .events import AgentEnd, AgentEvent
from .loop import Agent, to_user_message

log = logging.getLogger(__name__)

SubmitPolicy = Literal["queue", "steer", "follow_up", "collect"]
Delivery = Literal["started", "queued", "steered", "follow_up", "collected"]
AgentFactory = Callable[[str], Awaitable[Agent]]
EventSink = Callable[[str, AgentEvent], Awaitable[None]]


def merge_messages(messages: list[UserMessage]) -> UserMessage:
    """把多条用户消息合并成一条，文本之间换行分隔。"""
    content: list[TextBlock | ImageBlock] = []
    for i, m in enumerate(messages):
        if i > 0:
            content.append(TextBlock(text="\n"))
        content.extend(m.content)
    return UserMessage(content=content)


@dataclass
class _Lane:
    session_id: str
    queue: deque[list[Message]] = field(default_factory=deque[list[Message]])
    collect: list[UserMessage] = field(default_factory=list[UserMessage])
    agent: Agent | None = None
    worker: asyncio.Task[None] | None = None
    timer: asyncio.Task[None] | None = None
    running: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def busy(self) -> bool:
        return self.worker is not None or self.timer is not None


class LaneManager:
    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        on_event: EventSink | None = None,
        collect_window: float = 0.8,
        clock: Clock | None = None,
    ) -> None:
        self._factory = agent_factory
        self._on_event = on_event
        self.collect_window = collect_window
        self._clock = clock or SYSTEM_CLOCK
        self._lanes: dict[str, _Lane] = {}
        self._closed = False

    # ------------------------------------------------------------ 公共接口

    async def submit(
        self, session_id: str, message: str | UserMessage, policy: SubmitPolicy = "queue"
    ) -> Delivery:
        if self._closed:
            raise RuntimeError("LaneManager 已关闭")
        msg = to_user_message(message)
        lane = self._lanes.get(session_id)
        if lane is None:
            lane = self._lanes[session_id] = _Lane(session_id)

        if lane.running and lane.agent is not None and policy in ("steer", "follow_up"):
            if policy == "steer":
                lane.agent.steer(msg)
                return "steered"
            lane.agent.follow_up(msg)
            return "follow_up"
        if policy == "collect":
            lane.collect.append(msg)
            if lane.worker is None and lane.timer is None:
                lane.timer = asyncio.create_task(self._collect_timer(lane))
            return "collected"
        was_busy = lane.worker is not None
        lane.queue.append([msg])
        self._ensure_worker(lane)
        return "queued" if was_busy else "started"

    def is_running(self, session_id: str) -> bool:
        lane = self._lanes.get(session_id)
        return lane is not None and lane.running

    def abort(self, session_id: str, reason: str = "user") -> bool:
        lane = self._lanes.get(session_id)
        if lane is None or lane.agent is None or not lane.running:
            return False
        return lane.agent.abort(reason)

    @property
    def active_sessions(self) -> list[str]:
        return list(self._lanes)

    async def wait_idle(self, session_id: str | None = None) -> None:
        """等到指定会话（或全部会话）没有排队和运行中的工作。"""
        while True:
            if session_id is not None:
                lane = self._lanes.get(session_id)
                lanes = [lane] if lane is not None else []
            else:
                lanes = list(self._lanes.values())
            if not lanes:
                return
            await asyncio.gather(*(lane.done.wait() for lane in lanes))

    async def close(self) -> None:
        self._closed = True
        tasks: list[asyncio.Task[None]] = []
        for lane in list(self._lanes.values()):
            lane.queue.clear()
            lane.collect.clear()
            if lane.agent is not None and lane.running:
                lane.agent.abort("shutdown")
            for t in (lane.timer, lane.worker):
                if t is not None:
                    tasks.append(t)
            if lane.timer is not None:
                lane.timer.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._lanes.clear()

    # ------------------------------------------------------------ 内部

    def _ensure_worker(self, lane: _Lane) -> None:
        if lane.worker is None:
            lane.done.clear()
            lane.worker = asyncio.create_task(self._work(lane), name=f"lane:{lane.session_id}")

    async def _collect_timer(self, lane: _Lane) -> None:
        try:
            await self._clock.sleep(self.collect_window)
        finally:
            lane.timer = None
        self._flush_collect(lane)
        if lane.queue:
            self._ensure_worker(lane)
        else:
            self._maybe_reclaim(lane)

    def _flush_collect(self, lane: _Lane) -> None:
        if lane.collect:
            lane.queue.append([merge_messages(lane.collect)])
            lane.collect = []

    async def _work(self, lane: _Lane) -> None:
        try:
            while lane.queue:
                batch = lane.queue.popleft()
                agent: Agent | None = None
                lane.running = True
                try:
                    if lane.agent is None:
                        lane.agent = await self._factory(lane.session_id)
                    agent = lane.agent
                    async for ev in agent.run(batch):
                        await self._emit(lane.session_id, ev)
                except Exception as exc:  # 单次运行的意外错误不影响后续运行
                    log.exception("会话 %s 运行出错", lane.session_id)
                    await self._emit(
                        lane.session_id,
                        AgentEnd(run_id="lane", reason="error", error=f"{type(exc).__name__}: {exc}"),
                    )
                finally:
                    lane.running = False
                if agent is not None:
                    leftovers: list[Message] = list(agent.drain_queues())
                    if leftovers:  # 来不及注入的 steer / follow_up：作为下一次运行，不丢消息
                        lane.queue.appendleft(leftovers)
                if lane.collect:
                    if lane.timer is not None:
                        lane.timer.cancel()
                        lane.timer = None
                    self._flush_collect(lane)
        finally:
            lane.worker = None
            self._maybe_reclaim(lane)

    def _maybe_reclaim(self, lane: _Lane) -> None:
        if lane.busy or lane.queue or lane.collect:
            return
        if self._lanes.get(lane.session_id) is lane:
            del self._lanes[lane.session_id]  # 空闲即回收；下次由工厂重建 Agent
        lane.done.set()

    async def _emit(self, session_id: str, event: AgentEvent) -> None:
        if self._on_event is None:
            return
        try:
            await self._on_event(session_id, event)
        except Exception:
            log.exception("事件回调出错（会话 %s）", session_id)


__all__ = ["Delivery", "LaneManager", "SubmitPolicy", "merge_messages"]
