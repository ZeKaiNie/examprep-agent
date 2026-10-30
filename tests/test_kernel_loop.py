# -*- coding: utf-8 -*-
"""Agent loop tests: FakeProvider script, tool execution, gates and event log."""
from __future__ import annotations

import json
from pathlib import Path

from examprep_agent.core.context import ContextEngine
from examprep_agent.core.loop import EventLogger, LoopLimits, ToolRegistry, run_agent
from examprep_agent.core.provider import (
    ChatMessage,
    FakeProvider,
    ModelResponse,
    ProviderError,
    ToolCall,
    Usage,
)
from examprep_agent.security.risk import RiskLevel


def make_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(
        "echo",
        "Echo text back",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        lambda text: {"echo": text},
        risk=RiskLevel.L0,
    )
    tools.register("noop", "Do nothing", {"type": "object", "properties": {}}, lambda: {"ok": True},
                   risk=RiskLevel.L0)
    return tools


def test_loop_executes_tool_then_returns_final(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "agent.jsonl"
    provider = FakeProvider([
        {"content": "", "tool_calls": [{"name": "echo", "arguments": {"text": "stack"}}]},
        "A stack is LIFO [lec1.md p.1].",
    ])
    result = run_agent(
        provider=provider,
        task="Explain stacks",
        tools=make_tools(),
        logger=EventLogger(str(log_path)),
        session_id="test",
    )
    assert result.stopped == "final"
    assert result.content == "A stack is LIFO [lec1.md p.1]."
    assert [e["event"] for e in result.events] == [
        "session_start", "model_response", "tool_call", "tool_result", "model_response", "final",
    ]
    assert result.events[3]["ok"] is True
    assert json.loads(result.events[3]["observation"])["result"] == {"echo": "stack"}
    assert [m.role for m in result.messages] == ["system", "user", "assistant", "tool", "assistant"]

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(result.events)
    assert lines[0]["session_id"] == "test"
    assert all({"ts", "session_id", "event", "step"} <= set(line) for line in lines)


def test_max_steps_gate_stops_a_tool_loop() -> None:
    def always_tool(messages, tools):  # noqa: ANN001, ARG001
        return ModelResponse(tool_calls=(ToolCall(id="c1", name="noop", arguments={}),), usage=Usage(1, 1))

    provider = FakeProvider([always_tool] * 10)
    result = run_agent(
        provider=provider,
        task="loop forever",
        tools=make_tools(),
        limits=LoopLimits(max_steps=3, max_total_tokens=1_000_000),
    )
    assert result.stopped == "max_steps"
    assert len(provider.calls) == 3
    assert sum(1 for e in result.events if e["event"] == "tool_call") == 3
    budget = [e for e in result.events if e["event"] == "budget_exhausted"]
    assert budget and budget[-1]["gate"] == "max_steps"


def test_token_budget_gate_stops_the_run() -> None:
    script = [
        {"tool_calls": [{"name": "noop", "arguments": {}}], "usage": {"prompt_tokens": 90, "completion_tokens": 10}},
        {"tool_calls": [{"name": "noop", "arguments": {}}], "usage": {"prompt_tokens": 90, "completion_tokens": 10}},
        {"content": "should not be reached"},
    ]
    provider = FakeProvider(script)
    result = run_agent(
        provider=provider,
        task="budget",
        tools=make_tools(),
        limits=LoopLimits(max_steps=10, max_total_tokens=150),
    )
    assert result.stopped == "token_budget"
    assert len(provider.calls) == 2
    assert result.usage.total_tokens == 200
    # The second response pushed the run over budget: its tool must not execute.
    assert sum(1 for e in result.events if e["event"] == "tool_call") == 1
    assert sum(1 for e in result.events if e["event"] == "tool_result") == 1
    budget = [e for e in result.events if e["event"] == "budget_exhausted"]
    assert budget and budget[-1]["gate"] == "max_total_tokens"


def test_zero_usage_from_the_provider_cannot_bypass_the_token_gate() -> None:
    script = [
        {"tool_calls": [{"name": "noop", "arguments": {}}],
         "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        for _ in range(5)
    ]
    provider = FakeProvider(script)
    result = run_agent(
        provider=provider,
        task="budget",
        tools=make_tools(),
        limits=LoopLimits(max_steps=5, max_total_tokens=1),
    )
    assert result.stopped == "token_budget"
    assert result.usage.total_tokens > 0  # estimated, not a free pass
    assert len(provider.calls) == 1
    assert not any(e["event"] == "tool_call" for e in result.events)


def test_empty_prompts_with_zero_usage_still_cost_a_token() -> None:
    provider = FakeProvider([
        {"tool_calls": [{"name": "noop", "arguments": {}}]} for _ in range(3)
    ])
    result = run_agent(
        provider=provider,
        task="",
        tools=make_tools(),
        system_prompt="",
        limits=LoopLimits(max_steps=3, max_total_tokens=1),
    )
    assert result.stopped == "token_budget"
    assert len(provider.calls) == 1
    assert result.usage.total_tokens == 1


def test_an_over_budget_response_cannot_run_tools_with_side_effects() -> None:
    side_effects: list[str] = []
    tools = ToolRegistry()
    tools.register("write_file", "Pretend to write a file", {"type": "object", "properties": {}},
                   lambda: side_effects.append("written") or {"ok": True},
                   risk=RiskLevel.L1, access="write")
    provider = FakeProvider([
        {"tool_calls": [{"name": "write_file", "arguments": {}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 100}},
        "never reached",
    ])
    result = run_agent(
        provider=provider,
        task="budget",
        tools=tools,
        limits=LoopLimits(max_steps=5, max_total_tokens=150),
    )
    assert result.stopped == "token_budget"
    assert side_effects == []
    events = [e["event"] for e in result.events]
    assert events[-1] == "budget_exhausted"
    assert "tool_call" not in events and "tool_result" not in events


def test_oversized_observation_is_valid_json_and_marked_truncated() -> None:
    tools = ToolRegistry()
    tools.register("big", "Return a large result", {"type": "object", "properties": {}},
                   lambda: {"text": "x" * 5000}, risk=RiskLevel.L0)
    execution = tools.execute("big", {}, max_chars=200)
    assert execution.ok
    parsed = json.loads(execution.output)
    assert parsed["ok"] is True
    assert parsed["truncated"] is True
    assert isinstance(parsed["result"], str)
    assert len(execution.output) <= 200


def test_unknown_tool_becomes_an_observation_not_a_crash() -> None:
    provider = FakeProvider([
        {"tool_calls": [{"name": "does_not_exist", "arguments": {}}]},
        "Recovered without the tool.",
    ])
    result = run_agent(provider=provider, task="x", tools=make_tools())
    assert result.stopped == "final"
    assert result.content == "Recovered without the tool."
    tool_results = [e for e in result.events if e["event"] == "tool_result"]
    assert tool_results[0]["ok"] is False
    assert "unknown tool" in tool_results[0]["observation"]


def test_provider_error_is_a_structured_stop() -> None:
    class BrokenProvider:
        name = "broken"

        def complete(self, messages, tools=(), *, temperature=None):  # noqa: ANN001, ARG002
            raise ProviderError("boom")

    result = run_agent(provider=BrokenProvider(), task="x", tools=make_tools())
    assert result.stopped == "error"
    assert result.error == "boom"
    assert any(e["event"] == "provider_error" for e in result.events)


def test_tool_argument_errors_are_captured() -> None:
    provider = FakeProvider([
        {"tool_calls": [{"name": "echo", "arguments": {"nope": 1}}]},
        "done",
    ])
    result = run_agent(provider=provider, task="x", tools=make_tools())
    tool_results = [e for e in result.events if e["event"] == "tool_result"]
    assert tool_results[0]["ok"] is False
    assert "bad arguments" in tool_results[0]["observation"]


def test_registry_schema_and_decorator_form() -> None:
    tools = ToolRegistry()

    @tools.register("add", "Add two ints", {"type": "object", "properties": {"a": {}, "b": {}}},
                    risk=RiskLevel.L0)
    def add(a: int, b: int) -> int:
        return a + b

    assert "add" in tools and len(tools) == 1
    spec = tools.specs()[0]
    assert spec.name == "add" and spec.parameters["type"] == "object"
    assert json.loads(tools.execute("add", {"a": 2, "b": 3}).output)["result"] == 5


def test_fake_provider_drives_an_explain_then_quiz_script() -> None:
    """One scripted tutoring turn: search the material, build a quiz, explain and ask."""
    tools = ToolRegistry()
    tools.register(
        "search_materials", "Search course material",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        lambda query: [{"file": "ch1.md", "page": 1, "text": "A stack is LIFO."}],
        risk=RiskLevel.L0,
    )
    tools.register(
        "make_quiz", "Create a quiz item from the material",
        {"type": "object", "properties": {"chapter": {"type": "integer"}}},
        lambda chapter: {"qid": "q1", "question": "What order does a stack use?", "answer": "LIFO"},
        risk=RiskLevel.L0,
    )
    provider = FakeProvider([
        {"tool_calls": [{"id": "c1", "name": "search_materials", "arguments": {"query": "stack order"}}]},
        {"tool_calls": [{"id": "c2", "name": "make_quiz", "arguments": {"chapter": 1}}]},
        "A stack is LIFO [ch1.md p.1].\n\nQuiz: What order does a stack use?",
    ])
    result = run_agent(provider=provider, task="Teach stacks and quiz the student", tools=tools)

    assert result.stopped == "final"
    assert "ch1.md p.1" in result.content
    assert "Quiz:" in result.content
    calls = [e for e in result.events if e["event"] == "tool_call"]
    assert [e["tool"] for e in calls] == ["search_materials", "make_quiz"]
    observations = [e for e in result.events if e["event"] == "tool_result"]
    assert json.loads(observations[1]["observation"])["result"]["qid"] == "q1"


def test_prompt_includes_system_history_and_task() -> None:
    provider = FakeProvider(["ok"])
    result = run_agent(provider=provider, task="task text", tools=make_tools(), system_prompt="sys text")
    first_call = provider.calls[0][0]
    assert [m.role for m in first_call] == ["system", "user"]
    assert first_call[0].content == "sys text"
    assert first_call[1].content == "task text"
    assert result.content == "ok"


def test_compaction_provider_usage_counts_against_the_hard_budget() -> None:
    """I1: a compaction model call is real spend, not just a log field."""

    class CompactionProvider:
        name = "compaction-provider"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages, tools=(), *, temperature=None):  # noqa: ANN001, ARG002
            self.calls += 1
            if self.calls == 1:  # the summariser call
                return ModelResponse(content="a compact summary", usage=Usage(5000, 5000))
            return ModelResponse(content="final", usage=Usage(1, 1))

    provider = CompactionProvider()
    context = ContextEngine(max_tokens=200, provider=provider, keep_recent_messages=2)
    context.set_system("sys")
    context.extend([ChatMessage(role="user", content="old fact %d " % i + "x" * 200)
                    for i in range(8)])
    result = run_agent(
        provider=provider,
        task="teach",
        tools=make_tools(),
        context=context,
        limits=LoopLimits(max_steps=3, max_total_tokens=400),
    )
    assert result.stopped == "token_budget"
    assert provider.calls == 1  # the over-budget compaction call; no model turn ran
    assert result.usage.total_tokens == 10_000
    compaction = [e for e in result.events if e["event"] == "compaction"]
    assert compaction and compaction[0]["spent_tokens"] == 10_000


def test_context_engine_is_compacted_before_each_model_call(tmp_path: Path) -> None:
    log_path = tmp_path / "agent.jsonl"
    context = ContextEngine(max_tokens=200, keep_recent_messages=2)
    context.set_system("sys")
    context.set_material("material " * 300)
    context.extend([ChatMessage(role="user", content="old fact %d " % i + "x" * 200) for i in range(6)])
    provider = FakeProvider(["final answer"])
    result = run_agent(
        provider=provider,
        task="teach the slice",
        tools=make_tools(),
        context=context,
        logger=EventLogger(str(log_path)),
        session_id="ctx",
    )
    assert result.stopped == "final"
    compactions = [e for e in result.events if e["event"] == "compaction"]
    assert compactions, [e["event"] for e in result.events]
    assert "material_trimmed" in compactions[0] and "truncated_messages" in compactions[0]
    sent = provider.calls[0][0]
    material = next(m for m in sent if "COURSE MATERIAL" in m.content)
    assert "<<<MATERIAL " in material.content and "MATERIAL>>> " in material.content
    assert context.material_tokens() <= context.budget.material_tokens
    assert not context.over_budget()
    logged = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(entry["event"] == "compaction" for entry in logged)
