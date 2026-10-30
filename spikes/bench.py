# -*- coding: utf-8 -*-
"""Run the same three cases on the own kernel and on LangGraph; emit metrics.

Usage (from the repository root, with the spike extra installed)::

    python -m spikes.bench [--repeats 20]

Both adapters share :mod:`spikes.common`'s workload definitions, budget,
step counts and success predicates; each case performs the same number of
message/compaction steps on both sides.  The script never touches the network:
every model turn is a FakeProvider script.
"""
from __future__ import annotations

import argparse
import json
import os

from . import common
from . import kernel_adapter

try:
    from . import langgraph_adapter
    LANGGRAPH_AVAILABLE = True
except ImportError as exc:  # pragma: no cover - spike extra not installed
    langgraph_adapter = None  # type: ignore[assignment]
    LANGGRAPH_IMPORT_ERROR = str(exc)
    LANGGRAPH_AVAILABLE = False
else:
    LANGGRAPH_IMPORT_ERROR = ""

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KERNEL_FILES = [
    os.path.join(ROOT, "examprep_agent", "core", "loop.py"),
    os.path.join(ROOT, "examprep_agent", "core", "context.py"),
    os.path.join(ROOT, "examprep_agent", "core", "session.py"),
]
ADAPTER_FILES = {
    "kernel_adapter": os.path.join(ROOT, "spikes", "kernel_adapter.py"),
    "langgraph_adapter": os.path.join(ROOT, "spikes", "langgraph_adapter.py"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--json-out", default=os.path.join(ROOT, "spikes", "results.json"))
    args = parser.parse_args()

    outcomes = []
    for case in common.CASE_NAMES:
        outcomes.extend(kernel_adapter.run_case(case, args.repeats))
        if LANGGRAPH_AVAILABLE:
            outcomes.extend(langgraph_adapter.run_case(case, args.repeats))

    metrics = common.summarise(outcomes)
    report = {
        "repeats_per_case": args.repeats,
        "cases": list(common.CASE_NAMES),
        "methodology": {
            "same_workload": "one add+compact / graph-invoke per message on both sides",
            "same_budget": {
                "max_tokens": common.MAX_TOKENS,
                "system_tokens": common.BUDGET.system_tokens,
                "material_tokens": common.BUDGET.material_tokens,
                "history_tokens": common.BUDGET.history_tokens,
                "summary_tokens": common.SUMMARY_BUDGET_TOKENS,
                "keep_recent_messages": common.KEEP_RECENT,
            },
            "same_success_predicates": "spikes/common.py resume_success / compaction_success",
            "restart": "JSON serialise + deserialise into a fresh runtime on both sides",
            "note": "local FakeProvider micro-benchmark; not end-to-end model latency",
        },
        "metrics": metrics,
        "code_size": {
            "kernel_core_lines": common.line_count(KERNEL_FILES),
            **{name: common.line_count([path]) for name, path in ADAPTER_FILES.items()},
            "note": "adapter lines are not an equivalent implementation-cost comparison",
        },
        "langgraph_available": LANGGRAPH_AVAILABLE,
        "langgraph_import_error": LANGGRAPH_IMPORT_ERROR,
        "dependency_footprint": (
            common.dependency_footprint("langgraph") if LANGGRAPH_AVAILABLE else {}
        ),
        "outcomes": [outcome.to_dict() for outcome in outcomes],
    }
    common.dump_json(report, args.json_out)

    print("## Spike metrics\n")
    print("repeats/case: %d (FakeProvider only, no network)\n" % args.repeats)
    header = ("| case | ops | kernel success | kernel median ms | "
              "LangGraph success | LangGraph median ms |")
    print(header)
    print("| --- | --- | --- | --- | --- | --- |")
    for case in common.CASE_NAMES:
        kernel = metrics.get("kernel", {}).get(case, {})
        graph = metrics.get("langgraph", {}).get(case, {})
        print("| %s | %s | %s | %s | %s | %s |" % (
            case,
            kernel.get("operations", graph.get("operations", "—")),
            kernel.get("success_rate", "—"),
            kernel.get("median_latency_ms", "—"),
            graph.get("success_rate", "—"),
            graph.get("median_latency_ms", "—"),
        ))
    print("\nbudget: %s" % json.dumps(report["methodology"]["same_budget"]))
    print("code size: %s" % json.dumps(report["code_size"]))
    print("langgraph deps: %s" % json.dumps(report["dependency_footprint"]))
    if not LANGGRAPH_AVAILABLE:
        print("LangGraph missing: %s" % LANGGRAPH_IMPORT_ERROR)
    print("\nwrote %s" % args.json_out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
