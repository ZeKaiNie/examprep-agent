from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from examprep_llm import CancelToken, Gateway, ServerError, SystemMessage, UserMessage
from examprep_llm.testing import FakeClock, FakeProvider, FakeTurn
from examprep_runtime import ToolContext, tool
from examprep_runtime.subagent import NUDGE_TEXT, SUBMIT_TOOL, run_subagent


class GradeResult(BaseModel):
    score: float = Field(ge=0, le=10)
    hits: list[str]
    reason: str


GOOD = {"score": 8, "hits": ["定义正确"], "reason": "缺少推导"}


def gateway(*script: Any) -> tuple[Gateway, FakeProvider]:
    fake = FakeProvider(script)
    return Gateway({"fake": fake}, {"grader": ["fake/cheap"]}, clock=FakeClock()), fake


async def test_submit_result_success() -> None:
    gw, fake = gateway(FakeTurn(tool_calls=[(SUBMIT_TOOL, GOOD)]))
    prefix = [SystemMessage(content="L0 共享前缀", cacheable=True)]
    res = await run_subagent(
        gw,
        GradeResult,
        instructions="你是判分员",
        input="题目……学生答案……",
        role="grader",
        system_prefix=prefix,
    )
    assert res.ok
    assert res.value == GradeResult.model_validate(GOOD)
    assert res.reason == "completed"
    assert res.turns == 1
    assert res.usage.input_tokens == 10
    req = fake.requests[0]
    assert req.messages[0] == prefix[0]
    assert isinstance(req.messages[1], SystemMessage)
    assert req.messages[1].content == "你是判分员"
    spec = next(t for t in req.tools if t.name == SUBMIT_TOOL)
    assert set(spec.parameters["properties"]) == {"score", "hits", "reason"}


async def test_invalid_submission_then_fix() -> None:
    gw, fake = gateway(
        FakeTurn(tool_calls=[(SUBMIT_TOOL, {"score": 99, "hits": [], "reason": "x"})]),
        FakeTurn(tool_calls=[(SUBMIT_TOOL, GOOD)]),
    )
    res = await run_subagent(gw, GradeResult, instructions="判分", input="答案", role="grader")
    assert res.ok
    assert res.turns == 2
    assert len(fake.requests) == 2


async def test_nudge_when_no_submission() -> None:
    gw, fake = gateway(FakeTurn(text="我觉得 8 分"), FakeTurn(tool_calls=[(SUBMIT_TOOL, GOOD)]))
    res = await run_subagent(
        gw, GradeResult, instructions="判分", input=[UserMessage(content="答案")], role="grader"
    )  # type: ignore[arg-type]
    assert res.ok
    nudges = [m for m in res.messages if isinstance(m, UserMessage) and m.text == NUDGE_TEXT]
    assert len(nudges) == 1
    assert len(fake.requests) == 2


async def test_nudge_exhausted() -> None:
    gw, _ = gateway(FakeTurn(text="不提交"), FakeTurn(text="还是不提交"))
    res = await run_subagent(gw, GradeResult, instructions="判分", input="答案", role="grader")
    assert not res.ok
    assert res.value is None
    assert "没有调用" in (res.error or "")


async def test_timeout() -> None:
    gw, _ = gateway(FakeTurn(text="想很久", gate=asyncio.Event()))
    res = await asyncio.wait_for(
        run_subagent(gw, GradeResult, instructions="判分", input="答案", role="grader", timeout=0.05), 2
    )
    assert not res.ok
    assert res.error == "timeout"
    assert res.reason == "aborted"


async def test_llm_error_isolated() -> None:
    gw, _ = gateway(ServerError("down"), ServerError("down"), ServerError("down"))
    res = await run_subagent(gw, GradeResult, instructions="判分", input="答案", role="grader")
    assert not res.ok
    assert res.reason == "error"
    assert "down" in (res.error or "")


async def test_parent_cancel() -> None:
    parent = CancelToken()
    parent.cancel("父 Agent 取消")
    gw, fake = gateway(FakeTurn(tool_calls=[(SUBMIT_TOOL, GOOD)]))
    res = await run_subagent(gw, GradeResult, instructions="判分", input="答案", role="grader", cancel=parent)
    assert not res.ok
    assert res.error == "aborted"
    assert fake.requests == []


class Query(BaseModel):
    q: str


@tool(description="查参考答案")
async def lookup(params: Query, ctx: ToolContext) -> str:
    return "参考答案：……"


async def test_with_extra_tools_and_no_timeout() -> None:
    gw, fake = gateway(
        FakeTurn(tool_calls=[("lookup", {"q": "第 3 题"})]),
        FakeTurn(tool_calls=[(SUBMIT_TOOL, GOOD)]),
    )
    res = await run_subagent(
        gw, GradeResult, instructions="判分", input="答案", tools=[lookup], role="grader", timeout=None
    )
    assert res.ok
    assert res.turns == 2
    assert {t.name for t in fake.requests[0].tools} == {"lookup", SUBMIT_TOOL}
