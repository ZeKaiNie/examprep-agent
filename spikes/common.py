# -*- coding: utf-8 -*-
"""Shared measurement helpers for the kernel/LangGraph spike.

Both adapters run the *same* three worked examples with the *same* inputs,
budgets, step counts and success predicates:

1. ``resume`` — 12 message turns, each followed by one compaction step; the
   state is then serialised to JSON, restored into a fresh runtime and one more
   turn is processed.  Success: the earliest fact survives in the summary, the
   post-resume message is in history, and system/material/history stay inside
   the shared budget.
2. ``parallel_tools`` — one model turn requests three tools that each sleep
   50 ms; success means all three results come back.
3. ``compaction`` — 40 message turns with oversized system/material; each turn
   runs one compaction step.  Success: the earliest fact survives and every
   compartment is back inside the shared budget.

The workload definitions live here so neither adapter can drift from the other.
Nothing here is imported by the shipped package.
"""
from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from examprep_agent.core.context import ContextBudget, estimate_tokens

CASE_NAMES = ("resume", "parallel_tools", "compaction")

#: One budget for both sides (the kernel's three-compartment split).
MAX_TOKENS = 800
KEEP_RECENT = 4
SUMMARY_RATIO = 0.35
BUDGET: ContextBudget = ContextBudget.from_total(MAX_TOKENS)
HISTORY_BUDGET_TOKENS = BUDGET.history_tokens
SUMMARY_BUDGET_TOKENS = int(HISTORY_BUDGET_TOKENS * SUMMARY_RATIO)

RESUME_SYSTEM = "system prompt"
COMPACTION_SYSTEM = "system " * 400
COMPACTION_MATERIAL = "material " * 1200


def resume_messages(count: int = 12) -> list[str]:
    """Identical resume payload for both adapters."""
    return ["fact-%d: %s" % (index, "x" * 180) for index in range(count)]


def compaction_messages(count: int = 40) -> list[str]:
    """Identical compaction payload for both adapters."""
    return ["fact-%d: %s" % (index, "y" * 120) for index in range(count)]


def context_within_budget(summary: str, history: list[str],
                          system: str = "", material: str = "") -> bool:
    """The single success predicate for context size, used by both adapters."""
    history_tokens = estimate_tokens(summary + "\n" + "\n".join(history))
    return (
        history_tokens <= BUDGET.history_tokens
        and estimate_tokens(system) <= BUDGET.system_tokens
        and estimate_tokens(material) <= BUDGET.material_tokens
        and (estimate_tokens(system) + estimate_tokens(material) + history_tokens
             <= BUDGET.total_tokens)
    )


def resume_success(summary: str, history: list[str]) -> bool:
    """Identical resume criterion for both adapters."""
    return (
        "fact-0" in summary
        and "fact-after-resume" in history
        and context_within_budget(summary, history, RESUME_SYSTEM, "")
    )


def compaction_success(summary: str, history: list[str],
                       system: str, material: str) -> bool:
    """Identical compaction criterion for both adapters."""
    return (
        "fact-0" in summary
        and context_within_budget(summary, history, system, material)
    )


@dataclass
class CaseOutcome:
    """One repetition of one case on one adapter."""

    adapter: str
    case: str
    success: bool
    latency_ms: float
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def timed(fn: Callable[[], tuple[bool, dict[str, Any]]]) -> tuple[bool, float, dict[str, Any]]:
    """Run ``fn`` and return ``(success, latency_ms, detail)``."""
    started = time.perf_counter()
    success, detail = fn()
    return success, (time.perf_counter() - started) * 1000.0, detail


def summarise(outcomes: Iterable[CaseOutcome]) -> dict[str, Any]:
    """Per-adapter/per-case success rate, latency median and repetitions."""
    groups: dict[tuple[str, str], list[CaseOutcome]] = {}
    for outcome in outcomes:
        groups.setdefault((outcome.adapter, outcome.case), []).append(outcome)
    report: dict[str, Any] = {}
    for (adapter, case), rows in sorted(groups.items()):
        steps = [row.detail.get("steps") for row in rows if isinstance(row.detail.get("steps"), int)]
        report.setdefault(adapter, {})[case] = {
            "repeats": len(rows),
            "success_rate": sum(1 for row in rows if row.success) / len(rows),
            "median_latency_ms": round(statistics.median(row.latency_ms for row in rows), 3),
            "max_latency_ms": round(max(row.latency_ms for row in rows), 3),
            "operations": int(statistics.median(steps)) if steps else None,
        }
    return report


def dependency_footprint(root: str = "langgraph") -> dict[str, Any]:
    """Transitive dependency count measured from the *installed* environment.

    Only requirements whose environment markers evaluate true are followed,
    and a distribution is counted only if ``importlib.metadata.version`` can
    find it in this venv.  The earlier version skipped only ``extra ==``
    markers and counted names that were not installed at all (I7).
    """
    from importlib import metadata

    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - packaging ships with setuptools
        return {"root": root, "transitive_distributions": 0, "names": [],
                "error": "packaging is not installed"}

    environment = default_environment()
    environment["extra"] = ""
    seen: set[str] = set()
    queue = [root]
    missing: set[str] = set()
    while queue:
        name = queue.pop()
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in seen:
            continue
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.add(key)
            continue
        seen.add(key)
        try:
            requirements = metadata.requires(name) or []
        except metadata.PackageNotFoundError:  # pragma: no cover - raced removal
            continue
        for raw in requirements:
            try:
                requirement = Requirement(raw)
            except Exception:  # noqa: BLE001 - a malformed requirement is skipped
                continue
            if requirement.marker is not None and not requirement.marker.evaluate(environment):
                continue
            queue.append(requirement.name)
    return {
        "root": root,
        "transitive_distributions": len(seen),
        "names": sorted(seen),
        "not_installed": sorted(missing),
    }


def line_count(paths: Iterable[str]) -> int:
    """Physical line count of the listed files."""
    total = 0
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            total += sum(1 for _ in fh)
    return total


def dump_json(payload: Any, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
