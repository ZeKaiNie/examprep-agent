# -*- coding: utf-8 -*-
"""LangGraph side of the spike (StateGraph + InMemorySaver + Send fan-out).

The adapter deliberately mirrors the kernel's *business rules* using the same
shared budgets and the same deterministic summary helper: one graph invocation
per message performs exactly the same system/material trimming, tail selection
and summary truncation that ``ContextEngine.compact`` performs.  LangGraph
supplies graph execution/checkpointing; token accounting, compaction and the
success predicates remain hand-written, as the report states.
"""
from __future__ import annotations

import json
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from examprep_agent.core.context import (
    ChatMessage,
    ContextEngine,
    _sanitize_summary_text,
    _truncate_to_tokens,
    estimate_tokens,
)

from . import common
from .common import CaseOutcome, timed


def _merge_summary(previous: str, evicted: list[str]) -> str:
    """The kernel's deterministic summary, applied to LangGraph state strings."""
    helper = ContextEngine(max_tokens=common.MAX_TOKENS,
                           keep_recent_messages=common.KEEP_RECENT)
    helper.summary = previous
    text = helper._deterministic_summary(  # same rule as the kernel fallback
        [ChatMessage(role="user", content=message) for message in evicted])
    return _truncate_to_tokens(_sanitize_summary_text(text),
                               common.SUMMARY_BUDGET_TOKENS)


def _tail_size(history: list[str]) -> int:
    keep = min(common.KEEP_RECENT, len(history))
    tail_budget = max(0, int(common.HISTORY_BUDGET_TOKENS * 0.6))
    while keep > 1:
        if estimate_tokens("\n".join(history[-keep:])) <= tail_budget:
            break
        keep -= 1
    return keep


def _enforce_history_budget(summary: str, history: list[str]) -> list[str]:
    """Mirror ``ContextEngine._enforce_history_budget`` for string messages."""
    if estimate_tokens(summary + "\n" + "\n".join(history)) <= common.HISTORY_BUDGET_TOKENS:
        return history
    available = max(0, common.HISTORY_BUDGET_TOKENS - estimate_tokens(summary))
    kept: list[str | None] = [None] * len(history)
    remaining = available
    for index in range(len(history) - 1, -1, -1):
        needed = estimate_tokens(history[index])
        if needed <= remaining:
            kept[index] = history[index]
            remaining -= needed
        else:
            kept[index] = _truncate_to_tokens(history[index], remaining)
            remaining = 0
    result = [message for message in kept if message is not None]
    while (result
           and estimate_tokens(summary + "\n" + "\n".join(result))
           > common.HISTORY_BUDGET_TOKENS):
        if result[0]:
            result[0] = _truncate_to_tokens(result[0], 0)
        else:
            result.pop(0)
    return result


def _should_compact(system: str, material: str, summary: str, history: list[str]) -> bool:
    history_tokens = estimate_tokens(summary + "\n" + "\n".join(history))
    total = estimate_tokens(system) + estimate_tokens(material) + history_tokens
    return (
        history_tokens > common.HISTORY_BUDGET_TOKENS
        or estimate_tokens(system) > common.BUDGET.system_tokens
        or estimate_tokens(material) > common.BUDGET.material_tokens
        or total > common.BUDGET.total_tokens
    )


class SessionState(TypedDict, total=False):
    system: str
    material: str
    history: list[str]
    summary: str
    step: int
    incoming: str


def _compact_node(state: SessionState) -> dict[str, Any]:
    """One turn + one compaction step, matching the kernel's compact() rules."""
    system = str(state.get("system") or "")
    material = str(state.get("material") or "")
    history = list(state.get("history") or [])
    summary = str(state.get("summary") or "")
    incoming = str(state.get("incoming") or "")
    if incoming:
        history.append(incoming)

    if _should_compact(system, material, summary, history):
        if system and estimate_tokens(system) > common.BUDGET.system_tokens:
            system = _truncate_to_tokens(system, common.BUDGET.system_tokens)
        if material and estimate_tokens(material) > common.BUDGET.material_tokens:
            material = _truncate_to_tokens(material, common.BUDGET.material_tokens)
        if summary and estimate_tokens(summary) > common.SUMMARY_BUDGET_TOKENS:
            summary = _truncate_to_tokens(summary, common.SUMMARY_BUDGET_TOKENS)
        if history:
            keep = _tail_size(history)
            if keep < len(history):
                summary = _merge_summary(summary, history[:-keep])
                history = history[-keep:]
            history = _enforce_history_budget(summary, history)

    return {
        "system": system,
        "material": material,
        "history": history,
        "summary": summary,
        "step": int(state.get("step", 0)) + 1,
        "incoming": "",
    }


def _session_graph(saver: InMemorySaver | None = None):
    graph = StateGraph(SessionState)
    graph.add_node("turn", _compact_node)
    graph.add_edge(START, "turn")
    graph.add_edge("turn", END)
    return graph.compile(checkpointer=saver)


def _feed(app, config: dict[str, Any], messages: list[str],
          system: str = "", material: str = "") -> None:
    """One graph invocation per message (the shared workload unit)."""
    first = {"system": system, "material": material, "history": [],
             "summary": "", "step": 0, "incoming": messages[0]}
    app.invoke(first, config)
    for message in messages[1:]:
        app.invoke({"incoming": message}, config)


def case_resume() -> tuple[bool, dict[str, Any]]:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "spike-resume"}}
    app = _session_graph(saver)
    messages = common.resume_messages(12)
    _feed(app, config, messages, system=common.RESUME_SYSTEM)

    # Real restart simulation: only JSON bytes survive; a fresh graph/thread is
    # seeded from the deserialised state (this does not exercise the
    # checkpointer backend itself, which is stated in the report).
    state = app.get_state(config).values
    encoded = json.dumps({key: state.get(key) for key in
                          ("system", "material", "history", "summary", "step")},
                         ensure_ascii=False)
    restored = json.loads(encoded)
    resumed = _session_graph(InMemorySaver())
    resumed_config = {"configurable": {"thread_id": "spike-resume-restored"}}
    resumed.invoke({**restored, "incoming": "fact-after-resume"}, resumed_config)
    final = resumed.get_state(resumed_config).values

    history = list(final.get("history") or [])
    summary = str(final.get("summary") or "")
    success = common.resume_success(summary, history)
    return success, {
        "summary_tokens": estimate_tokens(summary),
        "kept_messages": len(history),
        "checkpoint_bytes": len(encoded.encode("utf-8")),
        "steps": len(messages),
        "history_tokens": estimate_tokens(summary + "\n" + "\n".join(history)),
    }


class ParallelState(TypedDict, total=False):
    results: Annotated[list[int], operator.add]


def _dispatch(state: ParallelState) -> list[Send]:
    return [Send("work", {"index": index}) for index in range(3)]


def _work(payload: dict[str, Any]) -> dict[str, Any]:
    time.sleep(0.05)
    return {"results": [int(payload["index"])]}


def case_parallel_tools() -> tuple[bool, dict[str, Any]]:
    graph = StateGraph(ParallelState)
    graph.add_node("work", _work)
    graph.add_conditional_edges(START, _dispatch, ["work"])
    graph.add_edge("work", END)
    app = graph.compile()
    result = app.invoke({"results": []})
    results = list(result.get("results") or [])
    return sorted(results) == [0, 1, 2], {"results": len(results), "steps": 3}


def case_compaction() -> tuple[bool, dict[str, Any]]:
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "spike-compaction"}}
    app = _session_graph(saver)
    messages = common.compaction_messages(40)
    _feed(app, config, messages,
          system=common.COMPACTION_SYSTEM, material=common.COMPACTION_MATERIAL)

    state = app.get_state(config).values
    history = list(state.get("history") or [])
    summary = str(state.get("summary") or "")
    system = str(state.get("system") or "")
    material = str(state.get("material") or "")
    success = common.compaction_success(summary, history, system, material)
    return success, {
        "history_tokens": estimate_tokens(summary + "\n" + "\n".join(history)),
        "system_tokens": estimate_tokens(system),
        "material_tokens": estimate_tokens(material),
        "summary_tokens": estimate_tokens(summary),
        "kept_messages": len(history),
        "steps": len(messages),
    }


CASES = {
    "resume": case_resume,
    "parallel_tools": case_parallel_tools,
    "compaction": case_compaction,
}


def run_case(case: str, repeats: int = 10) -> list[CaseOutcome]:
    runner = CASES[case]
    outcomes: list[CaseOutcome] = []
    for _ in range(repeats):
        success, latency, detail = timed(runner)
        outcomes.append(CaseOutcome("langgraph", case, success, latency, detail))
    return outcomes
