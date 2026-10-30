# -*- coding: utf-8 -*-
"""One-command offline benchmark entry point.

Usage::

    python -m benchmarks.run_all --out benchmarks/results/
    python -m benchmarks.run_all --out /tmp/bench --seed 20261030
    python -m benchmarks.run_all --judge        # only if an API key is set

Artifacts: ``results.json``, ``BENCH_REPORT.md`` and ``summary.md`` in ``--out``.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import DEFAULT_SEED
from .common import (
    RESULTS_DIR,
    cli_pointer,
    isolated_env,
    machine_info,
    write_json,
)
from .report import render_preview, render_report, summary_table


def _judge_provider(judge: bool, provider: Any | None) -> Any | None:
    """Resolve the judge provider *before* the offline sandbox strips API keys.

    ``isolated_env`` removes ``DEEPSEEK_API_KEY`` / ``OPENAI_API_KEY`` so the
    deterministic layers cannot accidentally call a real model.  The optional
    judge is the one layer that may use a provider, so its provider object must
    be built while the caller's environment is still visible (P3 review I7).
    """
    if not judge:
        return provider
    if provider is not None:
        return provider
    from examprep_agent.core.provider import provider_from_env

    return provider_from_env()


def run_all(*, out_dir: str | Path = RESULTS_DIR, seed: int = DEFAULT_SEED,
            judge: bool = False, performance: bool = True,
            command: str | None = None,
            judge_provider: Any | None = None) -> dict[str, Any]:
    """Run every layer offline and write the artifacts.  Returns the results dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    command = command or "python -m benchmarks.run_all --out %s --seed %d" % (out_dir, seed)
    judge_provider = _judge_provider(judge, judge_provider)

    with tempfile.TemporaryDirectory(prefix="examprep-bench-root-") as temp_root:
        root = Path(temp_root)
        with isolated_env(root / "env"), cli_pointer(root / "env" / "pointer"):
            from . import behavior, grounding, performance as performance_layer, tutor
            from .longhorizon import runner as longhorizon

            layers: dict[str, Any] = {
                "grounding": grounding.run(root / "grounding"),
                "behavior": behavior.run(root / "behavior"),
                "longhorizon": longhorizon.run(root / "longhorizon", seed=seed),
                "tutor": tutor.run(root / "tutor", judge=judge, provider=judge_provider),
            }
            if performance:
                layers["performance"] = performance_layer.run(root / "performance")
            else:
                layers["performance"] = {
                    "layer": "Performance",
                    "deterministic": False,
                    "skipped": True,
                    "note": "performance layer disabled with --no-performance",
                }

    results: dict[str, Any] = {
        "schema_version": 1,
        "meta": {
            "generated_at": generated_at,
            "command": command,
            "seed": seed,
            "machine": machine_info(),
            "deterministic_layers": ["grounding", "behavior", "longhorizon", "tutor"],
            "non_deterministic_layers": ["performance"],
            "note": "offline: no network provider is called on the default path",
        },
        "layers": layers,
    }
    write_json(out_dir / "results.json", results)
    report = render_report(results)
    (out_dir / "BENCH_REPORT.md").write_text(report, encoding="utf-8")
    (out_dir / "summary.md").write_text(summary_table(results), encoding="utf-8")
    # The canonical run also refreshes the marked README/P3 blocks, so the
    # published summary can never drift from this results.json.  Temporary
    # runs (tests, CI) leave the repository untouched.
    if out_dir.resolve() == RESULTS_DIR.resolve():
        from .report import sync_docs

        sync_docs(results)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the offline four-layer benchmark suite")
    parser.add_argument("--out", default=str(RESULTS_DIR),
                        help="output directory (default: benchmarks/results/)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED,
                        help="deterministic seed (default: %d)" % DEFAULT_SEED)
    parser.add_argument("--judge", action="store_true",
                        help="enable the optional LLM judge (skipped without an API key)")
    parser.add_argument("--no-performance", action="store_true",
                        help="skip timing probes (not used for acceptance)")
    args = parser.parse_args(argv)
    command = "python -m benchmarks.run_all --out %s --seed %d%s" % (
        args.out, args.seed, " --judge" if args.judge else "")
    results = run_all(out_dir=args.out, seed=args.seed, judge=args.judge,
                      performance=not args.no_performance, command=command)
    sys.stdout.write(render_preview(results))
    sys.stdout.write("wrote %s\n" % (Path(args.out) / "results.json"))
    sys.stdout.write("wrote %s\n" % (Path(args.out) / "BENCH_REPORT.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
