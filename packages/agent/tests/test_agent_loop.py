from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from kaopu_agent import (
    Agent,
    AgentEnd,
    AgentEvent,
    Budget,
    Hooks,
    MessageUpdate,
    ToolBlocked,
    ToolCallContext,
    ToolContext,
    ToolEnd,
    ToolOutput,
    ToolStart,
    ToolUpdate,
    TurnContext,
    TurnDecision,
    tool,
)
from kaopu_llm import (
    AssistantMessage,
    LLMRequest,
    ServerError,
    SystemMessage,
    TextBlock,
    TextDelta,
    ToolResultMessage,
    UserMessage,
)
from kaopu_llm.testing import FakeProvider, FakeTurn


async def collect(agent: Agent, input: Any = None, **kw: Any) -> list[AgentEvent]:
    return [ev async for ev in agent.run(input, **kw)]


def types(events: list[AgentEvent]) -> list[str]:
    return [e.type for e in events]


class CityArgs(BaseModel):
    city: str


@tool(description="查天气")
async def get_weather(params: CityArgs, ctx: ToolContext) -> str:
    ctx.update({"progress": 0.5})
    return f"{params.city}：晴，25 度"


def end_event(events: list[Any]) -> AgentEnd:
    last = events[-1]
    assert isinstance(last, AgentEnd)
    return last


# ---------------------------------------------------------------- 基本流程


async def test_text_only_event_sequence(make_agent: Any) -> None:
    agent, fake = make_agent([FakeTurn(text="你好", chunk_size=1)], system="你是助教")
    events = await collect(agent, "hi")
    assert types(events) == [
        "agent_start",
        "turn_start",
        "message_start",
        "message_end",
        "message_start",
        "message_update",  # start
        "message_update",  # 你
        "message_update",  # 好
        "message_update",  # done
        "message_end",
        "turn_end",
        "agent_end",
    ]
    end = end_event(events)
    assert end.reason == "completed"
    assert [m.role for m in end.new_messages] == ["user", "assistant"]
    req = fake.requests[0]
    assert isinstance(req.messages[0], SystemMessage)
    assert req.messages[0].cacheable
    assert not agent.running


async def test_tool_call_then_answer(make_agent: Any) -> None:
    agent, fake = make_agent(
        [FakeTurn(tool_calls=[("get_weather", {"city": "北京"}, "c1")]), FakeTurn(text="北京晴")],
        tools=[get_weather],
    )
    events = await collect(agent, "北京天气？")
    assert end_event(events).reason == "completed"
    assert [m.role for m in agent.messages] == ["user", "assistant", "tool_result", "assistant"]
    result = agent.messages[2]
    assert isinstance(result, ToolResultMessage)
    assert result.text == "北京：晴，25 度"
    assert not result.is_error
    # 第二次请求带上了工具声明和工具结果
    second = fake.requests[1]
    assert second.tools[0].name == "get_weather"
    assert second.tools[0].parameters["properties"] == {"city": {"title": "City", "type": "string"}}
    assert isinstance(second.messages[-1], ToolResultMessage)
    updates = [e for e in events if isinstance(e, ToolUpdate)]
    assert updates[0].data == {"progress": 0.5}
    starts = [e for e in events if isinstance(e, ToolStart)]
    assert starts[0].call.id == "c1"


async def test_input_variants(make_agent: Any) -> None:
    agent, _ = make_agent([FakeTurn(text="a"), FakeTurn(text="b"), FakeTurn(text="c")])
    await collect(agent, UserMessage(content=[TextBlock(text="1")]))
    await collect(agent, [UserMessage(content=[TextBlock(text="2")])])
    events = await collect(agent)  # 无新输入：基于现有对话继续
    assert end_event(events).reason == "completed"
    assert [m.role for m in agent.messages] == ["user", "assistant", "user", "assistant", "assistant"]


def test_constructor_validation(make_agent: Any) -> None:
    agent, _ = make_agent()
    with pytest.raises(ValueError, match="role"):
        Agent(agent.llm)
    with pytest.raises(ValueError, match="重名"):
        Agent(agent.llm, role="tutor", tools=[get_weather, get_weather])
    sys_agent = Agent(
        agent.llm, role="tutor", system=[SystemMessage(content="a"), SystemMessage(content="b")]
    )
    assert len(sys_agent.system) == 2


async def test_reentry_rejected(make_agent: Any) -> None:
    gate = asyncio.Event()
    agent, _ = make_agent([FakeTurn(text="x", gate=gate)])
    gen = agent.run("hi")
    await gen.__anext__()
    with pytest.raises(RuntimeError, match="重入"):
        await agent.run("again").__anext__()
    gate.set()
    rest = [e async for e in gen]
    assert end_event(rest).reason == "completed"


# ---------------------------------------------------------------- 工具执行


class NameArgs(BaseModel):
    name: str


async def test_parallel_tools_results_in_model_order(make_agent: Any) -> None:
    a_may_finish = asyncio.Event()

    @tool()
    async def work(params: NameArgs, ctx: ToolContext) -> str:
        """模拟耗时工具"""
        if params.name == "A":
            await a_may_finish.wait()  # A 必须等 B 执行后才能完成：证明二者并发
        else:
            a_may_finish.set()
        return f"done {params.name}"

    agent, _ = make_agent(
        [FakeTurn(tool_calls=[("work", {"name": "A"}), ("work", {"name": "B"})]), FakeTurn(text="ok")],
        tools=[work],
    )
    events = await asyncio.wait_for(collect(agent, "go"), 2)
    ends = [e for e in events if isinstance(e, ToolEnd)]
    assert [e.result.text for e in ends] == ["done B", "done A"]  # 完成顺序
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert [r.text for r in results] == ["done A", "done B"]  # 写回顺序 = 模型顺序
    assert work.description == "模拟耗时工具"


async def test_sequential_tool_runs_alone(make_agent: Any) -> None:
    log: list[str] = []
    active = 0

    async def body(name: str) -> str:
        nonlocal active
        active += 1
        log.append(f"start {name} active={active}")
        for _ in range(3):
            await asyncio.sleep(0)
        active -= 1
        log.append(f"end {name}")
        return name

    @tool(name="par")
    async def par(params: NameArgs, ctx: ToolContext) -> str:
        return await body(params.name)

    @tool(name="seq", sequential=True)
    async def seq(params: NameArgs, ctx: ToolContext) -> str:
        return await body(params.name)

    calls = [
        ("par", {"name": "p1"}),
        ("par", {"name": "p2"}),
        ("seq", {"name": "s"}),
        ("par", {"name": "p3"}),
    ]
    agent, _ = make_agent([FakeTurn(tool_calls=calls), FakeTurn(text="ok")], tools=[par, seq])
    await collect(agent, "go")
    assert log.index("start s active=1") > log.index("end p1")
    assert log.index("start s active=1") > log.index("end p2")
    assert log.index("start p3 active=1") > log.index("end s")
    assert "start p2 active=2" in log


async def test_tool_errors_are_reported_to_model(make_agent: Any) -> None:
    @tool()
    async def boom(params: CityArgs, ctx: ToolContext) -> str:
        """总是出错"""
        raise RuntimeError("磁盘坏了")

    @tool(timeout=0.01)
    async def slow(params: CityArgs, ctx: ToolContext) -> str:
        """总是超时"""
        await asyncio.sleep(10)
        return "never"

    script = [
        FakeTurn(
            tool_calls=[
                ("get_weather", {"town": "x"}),  # 校验失败
                ("nope", {}),  # 未知工具
                ("boom", {"city": "a"}),
                ("slow", {"city": "b"}),
            ]
        ),
        FakeTurn(tool_calls=[("get_weather", {})], raw_tool_arguments="{bad json"),
        FakeTurn(text="抱歉"),
    ]
    agent, fake = make_agent(script, tools=[get_weather, boom, slow])
    events = await collect(agent, "go")
    assert end_event(events).reason == "completed"
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert all(r.is_error for r in results)
    texts = [r.text for r in results]
    assert texts[0].startswith("参数校验失败")
    assert "city" in texts[0]
    assert "未知工具 'nope'" in texts[1]
    assert "磁盘坏了" in texts[2]
    assert "超时" in texts[3]
    assert "不是合法的 JSON" in texts[4]
    assert len(fake.requests) == 3


async def test_terminate_ends_after_batch(make_agent: Any) -> None:
    @tool()
    async def finish(params: CityArgs, ctx: ToolContext) -> ToolOutput:
        """提交并结束"""
        return ToolOutput(content="已提交", terminate=True, details={"city": params.city})

    agent, fake = make_agent([FakeTurn(tool_calls=[("finish", {"city": "x"})])], tools=[finish])
    events = await collect(agent, "go")
    assert end_event(events).reason == "completed"
    assert len(fake.requests) == 1
    end = next(e for e in events if isinstance(e, ToolEnd))
    assert end.details == {"city": "x"}


# ---------------------------------------------------------------- 钩子


class RecordingHooks(Hooks):
    def __init__(self, decision: TurnDecision = None) -> None:
        self.decision = decision
        self.turns: list[int] = []

    async def prepare_request(self, ctx: TurnContext, request: LLMRequest) -> LLMRequest:
        request.messages.insert(0, SystemMessage(content=f"第 {ctx.turn} 轮"))
        return request

    async def before_tool(self, ctx: ToolCallContext) -> ToolBlocked | None:
        assert isinstance(ctx.params, CityArgs)
        if ctx.params.city == "禁止":
            return ToolBlocked(reason="不允许查询")
        return None

    async def after_tool(self, ctx: ToolCallContext, output: ToolOutput) -> ToolOutput:
        return ToolOutput(content=f"[已审核] {output.content}")

    async def finish_turn(self, ctx: TurnContext) -> TurnDecision:
        self.turns.append(ctx.turn)
        return self.decision


async def test_hooks_prepare_before_after(make_agent: Any) -> None:
    hooks = RecordingHooks()
    agent, fake = make_agent(
        [
            FakeTurn(tool_calls=[("get_weather", {"city": "北京"}), ("get_weather", {"city": "禁止"})]),
            FakeTurn(text="ok"),
        ],
        tools=[get_weather],
        hooks=hooks,
    )
    await collect(agent, "go")
    first = fake.requests[0].messages[0]
    assert isinstance(first, SystemMessage)
    assert first.content == "第 1 轮"
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert results[0].text == "[已审核] 北京：晴，25 度"
    assert results[1].is_error
    assert "不允许查询" in results[1].text
    assert hooks.turns == [1, 2]


async def test_finish_turn_end_stops_even_with_tool_calls(make_agent: Any) -> None:
    agent, fake = make_agent(
        [FakeTurn(tool_calls=[("get_weather", {"city": "a"})])],
        tools=[get_weather],
        hooks=RecordingHooks("end"),
    )
    events = await collect(agent, "go")
    assert end_event(events).reason == "completed"
    assert len(fake.requests) == 1


async def test_finish_turn_continue_without_tools(make_agent: Any) -> None:
    class TwoTurns(Hooks):
        async def finish_turn(self, ctx: TurnContext) -> TurnDecision:
            return "continue" if ctx.turn == 1 else None

    agent, fake = make_agent([FakeTurn(text="一"), FakeTurn(text="二")], hooks=TwoTurns())
    await collect(agent, "go")
    assert len(fake.requests) == 2


async def test_hook_exception_ends_run_with_error(make_agent: Any) -> None:
    class Broken(Hooks):
        async def prepare_request(self, ctx: TurnContext, request: LLMRequest) -> LLMRequest:
            raise KeyError("配置缺失")

    agent, _ = make_agent([FakeTurn(text="x")], hooks=Broken())
    end = end_event(await collect(agent, "go"))
    assert end.reason == "error"
    assert "KeyError" in (end.error or "")
    assert not agent.running


# ---------------------------------------------------------------- steer / follow_up


async def test_steer_injected_after_tool_batch(make_agent: Any) -> None:
    @tool()
    async def lookup(params: CityArgs, ctx: ToolContext) -> str:
        """查资料，期间用户插话"""
        ctx.agent.steer("换成上海")
        return "北京资料"

    agent, fake = make_agent(
        [FakeTurn(tool_calls=[("lookup", {"city": "北京"})]), FakeTurn(text="好的，上海")], tools=[lookup]
    )
    await collect(agent, "查北京")
    roles = [m.role for m in agent.messages]
    assert roles == ["user", "assistant", "tool_result", "user", "assistant"]
    steer_msg = agent.messages[3]
    assert isinstance(steer_msg, UserMessage)
    assert steer_msg.text == "换成上海"
    assert fake.requests[1].messages[-1] == steer_msg


async def test_steer_without_tools_continues(make_agent: Any) -> None:
    gate = asyncio.Event()
    agent, fake = make_agent([FakeTurn(text="第一", gate=gate), FakeTurn(text="第二")])
    async for ev in agent.run("hi"):
        if isinstance(ev, MessageUpdate) and isinstance(ev.event, TextDelta) and not gate.is_set():
            agent.steer("插话")
            gate.set()
    assert len(fake.requests) == 2
    assert [m.role for m in agent.messages] == ["user", "assistant", "user", "assistant"]


async def test_follow_up_after_natural_end(make_agent: Any) -> None:
    agent, fake = make_agent([FakeTurn(text="答一"), FakeTurn(text="答二")])
    agent.follow_up("再讲一遍")
    assert agent.pending_messages == 1
    await collect(agent, "问题")
    assert [m.role for m in agent.messages] == ["user", "assistant", "user", "assistant"]
    assert len(fake.requests) == 2
    assert agent.drain_queues() == []


async def test_drain_queues(make_gateway: Any) -> None:
    agent = Agent(make_gateway(FakeProvider()), role="tutor")
    agent.steer("a")
    agent.follow_up(UserMessage(content=[TextBlock(text="b")]))
    assert [m.text for m in agent.drain_queues()] == ["a", "b"]
    assert agent.pending_messages == 0
    assert not agent.abort()  # 未运行时 abort 返回 False


# ---------------------------------------------------------------- 取消


async def test_abort_mid_stream_keeps_partial_text(make_agent: Any) -> None:
    gate = asyncio.Event()
    agent, _ = make_agent([FakeTurn(text="第一句。第二句。", chunk_size=4, gate=gate), FakeTurn(text="继续")])
    events: list[Any] = []

    async def consume() -> None:
        async for ev in agent.run("讲"):
            events.append(ev)
            if isinstance(getattr(ev, "event", None), TextDelta):
                assert agent.abort("学生点了停止")

    await asyncio.wait_for(consume(), 2)
    end = end_event(events)
    assert end.reason == "aborted"
    last = agent.messages[-1]
    assert isinstance(last, AssistantMessage)
    assert last.stop_reason == "aborted"
    assert last.text == "第一句。"
    assert last.error == "学生点了停止"
    # 可以从中断处继续
    events = await collect(agent, "继续")
    assert end_event(events).reason == "completed"


async def test_abort_during_tool_execution(make_agent: Any) -> None:
    started = asyncio.Event()

    @tool()
    async def forever(params: CityArgs, ctx: ToolContext) -> str:
        """永不返回"""
        started.set()
        await asyncio.Event().wait()
        return "never"

    agent, fake = make_agent(
        [FakeTurn(tool_calls=[("forever", {"city": "x"}), ("get_weather", {"city": "y"})])],
        tools=[forever, get_weather],
    )

    async def stopper() -> None:
        await started.wait()
        agent.abort()

    stop_task = asyncio.create_task(stopper())
    events = await asyncio.wait_for(collect(agent, "go"), 2)
    await stop_task
    assert end_event(events).reason == "aborted"
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert len(results) == 2  # 每个调用都有结果，对话保持合法
    assert results[0].is_error
    assert "已取消" in results[0].text
    assert len(fake.requests) == 1


async def test_abort_via_external_token(make_agent: Any) -> None:
    from kaopu_llm import CancelToken

    token = CancelToken()
    token.cancel("外部取消")
    agent, fake = make_agent([FakeTurn(text="x")])
    agent.steer("留着")
    events = await collect(agent, "hi", cancel=token)
    assert end_event(events).reason == "aborted"
    assert fake.requests == []
    assert agent.pending_messages == 2  # 输入与插话都放回队列


async def test_abort_between_tool_groups(make_agent: Any) -> None:
    @tool(sequential=True)
    async def first(params: CityArgs, ctx: ToolContext) -> str:
        """第一组"""
        ctx.agent.abort()
        return "1"

    agent, _ = make_agent(
        [FakeTurn(tool_calls=[("first", {"city": "a"}), ("get_weather", {"city": "b"})])],
        tools=[first, get_weather],
    )
    events = await collect(agent, "go")
    assert end_event(events).reason == "aborted"
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert results[0].text == "1"
    assert "已取消" in results[1].text


# ---------------------------------------------------------------- 预算与错误


async def test_max_turns(make_agent: Any) -> None:
    script = [FakeTurn(tool_calls=[("get_weather", {"city": str(i)})]) for i in range(5)]
    agent, fake = make_agent(script, tools=[get_weather], budget=Budget(max_turns=2))
    events = await collect(agent, "go")
    assert end_event(events).reason == "max_turns"
    assert len(fake.requests) == 2


async def test_max_tool_calls(make_agent: Any) -> None:
    agent, fake = make_agent(
        [FakeTurn(tool_calls=[("get_weather", {"city": "a"}), ("get_weather", {"city": "b"})])],
        tools=[get_weather],
        budget=Budget(max_tool_calls=1),
    )
    events = await collect(agent, "go")
    assert end_event(events).reason == "max_tool_calls"
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert not results[0].is_error
    assert results[1].is_error
    assert "上限" in results[1].text
    assert len(fake.requests) == 1


async def test_loop_detection(make_agent: Any) -> None:
    calls = 0

    @tool()
    async def same(params: CityArgs, ctx: ToolContext) -> str:
        """重复调用"""
        nonlocal calls
        calls += 1
        return "同样的结果"

    script = [FakeTurn(tool_calls=[("same", {"city": "x"})]) for _ in range(5)]
    agent, fake = make_agent(script, tools=[same])
    events = await collect(agent, "go")
    assert end_event(events).reason == "loop_detected"
    assert calls == 2
    assert len(fake.requests) == 3
    last = agent.messages[-1]
    assert isinstance(last, ToolResultMessage)
    assert "重复调用" in last.text


async def test_loop_detection_skips_rest_of_batch(make_agent: Any) -> None:
    script = [FakeTurn(tool_calls=[("get_weather", {"city": "x"})] * 3 + [("get_weather", {"city": "y"})])]
    agent, _ = make_agent(script, tools=[get_weather])
    events = await collect(agent, "go")
    assert end_event(events).reason == "loop_detected"
    results = [m for m in agent.messages if isinstance(m, ToolResultMessage)]
    assert [r.is_error for r in results] == [False, False, True, True]
    assert "中止" in results[3].text


async def test_llm_error_ends_run(make_agent: Any) -> None:
    agent, _ = make_agent([ServerError("挂了"), ServerError("挂了"), ServerError("挂了")])
    events = await collect(agent, "go")
    end = end_event(events)
    assert end.reason == "error"
    assert "挂了" in (end.error or "")
    last = agent.messages[-1]
    assert isinstance(last, AssistantMessage)
    assert last.stop_reason == "error"


def test_tool_decorator_validation() -> None:
    with pytest.raises(TypeError, match="Pydantic"):

        @tool()
        async def bad(params: int, ctx: ToolContext) -> str:  # pyright: ignore[reportUnusedFunction]
            return ""
