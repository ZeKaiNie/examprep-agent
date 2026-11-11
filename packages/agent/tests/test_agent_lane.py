from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kaopu_agent import Agent, AgentEnd, AgentEvent, AgentStart, MessageUpdate
from kaopu_agent.lane import LaneManager, merge_messages
from kaopu_llm import Gateway, ImageBlock, TextBlock, TextDelta, UserMessage
from kaopu_llm.testing import FakeClock, FakeProvider, FakeTurn


class World:
    """每个会话一个 Agent + FakeProvider；记录所有事件。"""

    def __init__(
        self, scripts: dict[str, list[FakeTurn]] | None = None, *, fail_first: set[str] | None = None
    ) -> None:
        self.scripts = scripts or {}
        self.fakes: dict[str, FakeProvider] = {}
        self.agents: dict[str, Agent] = {}
        self.events: list[tuple[str, AgentEvent]] = []
        self.fail_first = set(fail_first or ())
        self.factory_calls = 0

    async def factory(self, sid: str) -> Agent:
        self.factory_calls += 1
        if sid in self.fail_first:
            self.fail_first.discard(sid)
            raise RuntimeError("加载会话失败")
        if sid not in self.agents:
            fake = FakeProvider(self.scripts.get(sid, []), default=FakeTurn(text="默认回答"))
            self.fakes[sid] = fake
            gw = Gateway({"fake": fake}, {"tutor": ["fake/m"]}, clock=FakeClock())
            self.agents[sid] = Agent(gw, role="tutor")
        return self.agents[sid]

    async def sink(self, sid: str, ev: AgentEvent) -> None:
        self.events.append((sid, ev))

    def kinds(self, sid: str, *names: str) -> list[str]:
        return [e.type for s, e in self.events if s == sid and e.type in names]

    def ends(self, sid: str) -> list[AgentEnd]:
        return [e for s, e in self.events if s == sid and isinstance(e, AgentEnd)]


async def ticks(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def manager(world: World, **kw: Any) -> LaneManager:
    kw.setdefault("clock", FakeClock())
    return LaneManager(world.factory, on_event=world.sink, **kw)


async def test_same_session_runs_serially() -> None:
    g1, g2 = asyncio.Event(), asyncio.Event()
    world = World({"s": [FakeTurn(text="一", gate=g1), FakeTurn(text="二", gate=g2)]})
    lm = manager(world)
    assert await lm.submit("s", "问题一") == "started"
    assert await lm.submit("s", "问题二") == "queued"
    await ticks()
    assert lm.is_running("s")
    assert world.fakes["s"].active_streams == 1
    assert world.kinds("s", "agent_start", "agent_end") == ["agent_start"]
    g1.set()
    await ticks()
    assert world.kinds("s", "agent_start", "agent_end") == ["agent_start", "agent_end", "agent_start"]
    g2.set()
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert world.fakes["s"].max_active_streams == 1
    texts = [getattr(m, "text", "") for m in world.agents["s"].messages]
    assert texts == ["问题一", "一", "问题二", "二"]
    assert lm.active_sessions == []  # 空闲即回收
    assert not lm.is_running("s")


async def test_different_sessions_run_in_parallel() -> None:
    gate = asyncio.Event()
    world = World({"a": [FakeTurn(text="a", gate=gate)], "b": [FakeTurn(text="b", gate=gate)]})
    lm = manager(world)
    await lm.submit("a", "x")
    await lm.submit("b", "y")
    await ticks()
    assert world.fakes["a"].active_streams == 1
    assert world.fakes["b"].active_streams == 1
    assert sorted(lm.active_sessions) == ["a", "b"]
    gate.set()
    await asyncio.wait_for(lm.wait_idle(), 2)
    assert [e.reason for e in world.ends("a") + world.ends("b")] == ["completed", "completed"]


async def _wait_streaming(world: World, sid: str) -> None:
    for _ in range(100):
        if any(
            s == sid and isinstance(e, MessageUpdate) and isinstance(e.event, TextDelta)
            for s, e in world.events
        ):
            return
        await asyncio.sleep(0)
    raise AssertionError("未开始输出")


async def test_steer_while_running() -> None:
    gate = asyncio.Event()
    world = World({"s": [FakeTurn(text="讲解中", gate=gate), FakeTurn(text="好，换个例子")]})
    lm = manager(world)
    await lm.submit("s", "讲一下")
    await _wait_streaming(world, "s")
    assert await lm.submit("s", "能换个例子吗", "steer") == "steered"
    gate.set()
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert world.kinds("s", "agent_start") == ["agent_start"]  # 同一次运行内完成
    roles = [m.role for m in world.agents["s"].messages]
    assert roles == ["user", "assistant", "user", "assistant"]


async def test_follow_up_while_running() -> None:
    gate = asyncio.Event()
    world = World({"s": [FakeTurn(text="答", gate=gate), FakeTurn(text="再答")]})
    lm = manager(world)
    await lm.submit("s", "问")
    await _wait_streaming(world, "s")
    assert await lm.submit("s", "追问", "follow_up") == "follow_up"
    gate.set()
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert world.kinds("s", "agent_start") == ["agent_start"]
    assert len(world.fakes["s"].requests) == 2


async def test_collect_while_running_merges() -> None:
    gate = asyncio.Event()
    world = World({"s": [FakeTurn(text="答", gate=gate)]})
    lm = manager(world)
    await lm.submit("s", "问")
    await _wait_streaming(world, "s")
    assert await lm.submit("s", "补充一", "collect") == "collected"
    assert await lm.submit("s", "补充二", "collect") == "collected"
    gate.set()
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert world.kinds("s", "agent_start") == ["agent_start", "agent_start"]
    merged = world.agents["s"].messages[2]
    assert isinstance(merged, UserMessage)
    assert merged.text == "补充一\n补充二"


async def test_collect_when_idle_waits_window() -> None:
    clock = FakeClock()
    world = World()
    lm = manager(world, clock=clock, collect_window=0.5)
    assert await lm.submit("s", "第一条", "collect") == "collected"
    assert await lm.submit("s", "第二条", "collect") == "collected"
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert clock.sleeps == [0.5]
    assert world.kinds("s", "agent_start") == ["agent_start"]
    first = world.agents["s"].messages[0]
    assert isinstance(first, UserMessage)
    assert first.text == "第一条\n第二条"


async def test_late_follow_up_is_not_lost() -> None:
    """运行收尾时才到的 follow_up：Agent 已来不及注入，lane 取出后作为下一次运行。"""
    world = World({"s": [FakeTurn(text="答"), FakeTurn(text="补答")]})
    lm: LaneManager

    async def sink(sid: str, ev: AgentEvent) -> None:
        world.events.append((sid, ev))
        if isinstance(ev, AgentEnd) and len(world.ends(sid)) == 1:
            assert await lm.submit(sid, "迟到的追问", "follow_up") == "follow_up"

    lm = LaneManager(world.factory, on_event=sink, clock=FakeClock())
    await lm.submit("s", "问")
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert world.kinds("s", "agent_start") == ["agent_start", "agent_start"]
    assert [getattr(m, "text", "") for m in world.agents["s"].messages] == ["问", "答", "迟到的追问", "补答"]


async def test_errors_are_isolated() -> None:
    world = World(fail_first={"s"})

    async def broken_sink(sid: str, ev: AgentEvent) -> None:
        world.events.append((sid, ev))
        if isinstance(ev, AgentStart):
            raise ValueError("前端断开")

    lm = LaneManager(world.factory, on_event=broken_sink, clock=FakeClock())
    await lm.submit("s", "一")
    await lm.submit("s", "二")
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    ends = world.ends("s")
    assert ends[0].reason == "error"
    assert "加载会话失败" in (ends[0].error or "")
    assert ends[1].reason == "completed"  # 后续运行不受影响
    assert world.factory_calls == 2


async def test_abort_and_close() -> None:
    gate = asyncio.Event()
    world = World({"s": [FakeTurn(text="长篇", gate=gate)]})
    lm = manager(world)
    assert not lm.abort("s")
    await lm.submit("s", "讲")
    await _wait_streaming(world, "s")
    assert lm.abort("s")
    await asyncio.wait_for(lm.wait_idle("s"), 2)
    assert world.ends("s")[0].reason == "aborted"

    gate2 = asyncio.Event()
    world2 = World({"t": [FakeTurn(text="x", gate=gate2)]})
    lm2 = manager(world2)
    await lm2.submit("t", "1")
    await lm2.submit("t", "2")
    await lm2.submit("u", "c", "collect")
    await _wait_streaming(world2, "t")
    await asyncio.wait_for(lm2.close(), 2)
    assert world2.ends("t")[0].reason == "aborted"
    assert len(world2.ends("t")) == 1  # 排队的第二条被丢弃
    with pytest.raises(RuntimeError, match="已关闭"):
        await lm2.submit("t", "3")


def test_merge_messages_keeps_images() -> None:
    a = UserMessage(content=[TextBlock(text="看图"), ImageBlock(media_type="image/png", data="x")])
    b = UserMessage(content=[TextBlock(text="这题怎么做")])
    merged = merge_messages([a, b])
    assert [blk.type for blk in merged.content] == ["text", "image", "text", "text"]
    assert merged.text == "看图\n这题怎么做"
