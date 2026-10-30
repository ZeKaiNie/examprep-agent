# -*- coding: utf-8 -*-
"""Own-kernel side of the spike (ContextEngine + ToolRegistry + run_agent).

Every case uses the shared workload and budget from :mod:`spikes.common` and
performs exactly one ``ContextEngine.add`` + one ``compact`` step per message,
mirroring LangGraph's one graph invocation per message.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from examprep_agent.core.context import ContextEngine
from examprep_agent.core.loop import LoopLimits, ToolRegistry, run_agent
from examprep_agent.core.provider import ChatMessage, FakeProvider
from examprep_agent.security.risk import RiskLevel

from . import common
from .common import CaseOutcome, timed


def _new_engine(system: str = "", material: str = "") -> ContextEngine:
    engine = ContextEngine(max_tokens=common.MAX_TOKENS,
                           keep_recent_messages=common.KEEP_RECENT)
    if system:
        engine.set_system(system)
    if material:
        engine.set_material(material)
    return engine


def _feed(engine: ContextEngine, messages: list[str]) -> None:
    """One add + one compaction step per message (the shared workload unit)."""
    for message in messages:
        engine.add(ChatMessage(role="user", content=message))
        engine.compact()


def _checkpoint(engine: ContextEngine) -> dict[str, Any]:
    """What the kernel persists across a process boundary (session.json shape)."""
    return {
        "system": engine.system,
        "material": engine.material,
        "summary": engine.summary,
        "history": [asdict(message) for message in engine.history],
    }


def _restore(checkpoint: dict[str, Any]) -> ContextEngine:
    engine = ContextEngine(max_tokens=common.MAX_TOKENS,
                           keep_recent_messages=common.KEEP_RECENT)
    engine.set_system(str(checkpoint.get("system") or ""))
    engine.set_material(str(checkpoint.get("material") or ""))
    engine.summary = str(checkpoint.get("summary") or "")
    engine.extend([ChatMessage(**payload) for payload in checkpoint.get("history") or []])
    return engine


def case_resume() -> tuple[bool, dict[str, Any]]:
    engine = _new_engine(common.RESUME_SYSTEM)
    messages = common.resume_messages(12)
    _feed(engine, messages)
    # Real serialisation: JSON bytes are the only thing that survives.
    encoded = json.dumps(_checkpoint(engine), ensure_ascii=False)
    resumed = _restore(json.loads(encoded))
    resumed.add(ChatMessage(role="user", content="fact-after-resume"))
    resumed.compact()

    history = [message.content for message in resumed.history]
    success = common.resume_success(resumed.summary, history)
    return success, {
        "summary_tokens": resumed.summary_tokens(),
        "kept_messages": len(resumed.history),
        "checkpoint_bytes": len(encoded.encode("utf-8")),
        "steps": len(messages),
        "history_tokens": resumed.history_tokens(),
    }


def case_parallel_tools() -> tuple[bool, dict[str, Any]]:
    tools = ToolRegistry()

    def sleepy(index: int) -> dict[str, Any]:
        time.sleep(0.05)
        return {"index": index}

    tools.register(
        "sleepy", "Sleep briefly",
        {"type": "object", "properties": {"index": {"type": "integer"}}},
        sleepy, risk=RiskLevel.L0,
    )
    provider = FakeProvider([
        {"tool_calls": [
            {"id": "c1", "name": "sleepy", "arguments": {"index": 0}},
            {"id": "c2", "name": "sleepy", "arguments": {"index": 1}},
            {"id": "c3", "name": "sleepy", "arguments": {"index": 2}},
        ]},
        "all tools done",
    ])
    result = run_agent(
        provider=provider, task="call the tools", tools=tools,
        limits=LoopLimits(max_steps=3, max_total_tokens=10_000),
    )
    observed = [event["tool"] for event in result.events if event["event"] == "tool_result"]
    return (
        result.stopped == "final" and len(observed) == 3,
        {"tool_results": len(observed), "stopped": result.stopped, "steps": 3},
    )


def case_compaction() -> tuple[bool, dict[str, Any]]:
    engine = _new_engine(common.COMPACTION_SYSTEM, common.COMPACTION_MATERIAL)
    messages = common.compaction_messages(40)
    _feed(engine, messages)

    history = [message.content for message in engine.history]
    success = common.compaction_success(engine.summary, history,
                                        engine.system, engine.material)
    return success, {
        "history_tokens": engine.history_tokens(),
        "material_tokens": engine.material_tokens(),
        "system_tokens": engine.system_tokens(),
        "summary_tokens": engine.summary_tokens(),
        "kept_messages": len(engine.history),
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
        outcomes.append(CaseOutcome("kernel", case, success, latency, detail))
    return outcomes
