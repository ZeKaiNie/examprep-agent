# -*- coding: utf-8 -*-
"""Shared helpers for the offline benchmark suite.

Everything here is deterministic: no wall-clock value is used for a scored
metric, no network call is made, and temporary workspaces are always created
under one benchmark-owned root.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import platform
import shutil
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_COURSE = REPO_ROOT / "tests" / "fixtures" / "zh-data-structures"
DATA_DIR = Path(__file__).resolve().parent / "data"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

ENV_KEYS = (
    "EXAMPREP_MODEL",
    "EXAMPREP_BASE_URL",
    "EXAMPREP_WORKSPACE",
    "EXAMPREP_OFFLINE",
    "EXAMPREP_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "EXAM_CRAM_WORKSPACE",
    "EXAMPREP_INGEST_WORKERS",
)


def json_dumps(value: Any) -> str:
    """Stable, human-readable JSON (sorted keys for reproducible diffs)."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_json(path: str | Path, value: Any) -> None:
    """Write JSON with a trailing newline and UTF-8 encoding."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(value), encoding="utf-8")


def read_json(path: str | Path) -> Any:
    """Read JSON."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def copy_sample_course(dest: str | Path) -> Path:
    """Copy the bundled sample course to ``dest`` and return the copy."""
    dest = Path(dest)
    shutil.copytree(SAMPLE_COURSE, dest)
    return dest


@contextlib.contextmanager
def isolated_env(root: str | Path) -> Iterator[Path]:
    """Force the benchmark into an offline, throwaway configuration sandbox.

    The caller's environment is restored afterwards so running the benchmark
    from a configured shell cannot leak a real API key into a run (and vice
    versa).
    """
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    root = Path(root)
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["EXAMPREP_OFFLINE"] = "1"
        os.environ["EXAMPREP_CONFIG_DIR"] = str(root / "config")
        os.environ["EXAMPREP_WORKSPACE"] = str(root / "workspace")
        yield root
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def cli_pointer(path: str | Path) -> Iterator[None]:
    """Point the in-process CLI at a benchmark-owned workspace pointer."""
    from examprep_agent import cli

    saved = cli.POINTER
    cli.POINTER = str(path)
    try:
        yield
    finally:
        cli.POINTER = saved


@dataclass
class CommandResult:
    """Captured outcome of one in-process CLI invocation."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    @property
    def last_line(self) -> str:
        lines = self.stdout.rstrip().splitlines()
        return lines[-1] if lines else ""

    @property
    def has_footer(self) -> bool:
        return self.last_line.startswith("📍") and "examprep " in self.last_line


def run_cli(*argv: str) -> CommandResult:
    """Run ``examprep`` in-process with captured stdout/stderr."""
    from examprep_agent import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return CommandResult(tuple(argv), int(code), out.getvalue(), err.getvalue())


def material_pages(materials: str | Path) -> list[dict[str, Any]]:
    """Extract every course page with its file/page provenance (offline)."""
    from examprep_agent.tools import extract

    materials = str(materials)
    pages: list[dict[str, Any]] = []
    for source in extract.scan(materials):
        if source.error:
            continue
        for page in source.pages:
            pages.append({
                "file": source.rel,
                "page": int(page.number),
                "text": page.text,
            })
    return pages


def page_text(pages: Sequence[dict[str, Any]], file: str, page: int) -> str | None:
    """Text of one (file, page), or None when that page does not exist."""
    for item in pages:
        if item["file"] == file and int(item["page"]) == page:
            return str(item["text"])
    return None


def valid_page(pages: Sequence[dict[str, Any]], file: str, page: int) -> bool:
    """Whether ``file p.page`` is a real, extractable page in the course."""
    return page_text(pages, file, page) is not None


def fmt(value: Any) -> str:
    """Format a report value without inventing precision."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return "%.4f" % value
    return str(value)


def pct(numerator: int, denominator: int) -> float | None:
    """Percentage in [0, 100] rounded to four decimals, or None for 0/0."""
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 4)


def mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 6) if values else None


def median(values: Sequence[float]) -> float | None:
    return round(statistics.median(values), 6) if values else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Simple nearest-rank percentile (q in [0, 100])."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 6)
    index = max(0, min(len(ordered) - 1, int(round((q / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 6)


def machine_info() -> dict[str, Any]:
    """Describe the current container/VM used for the performance micro-benchmarks."""
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "note": "current container; absolute latency is machine-specific",
    }


def date_range(start: date, days: int) -> list[date]:
    """Consecutive dates starting at ``start``."""
    return [start + timedelta(days=offset) for offset in range(days)]


def tolerance_equal(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    """Recursively compare benchmark payloads with a float tolerance."""
    if isinstance(left, float) or isinstance(right, float):
        try:
            return abs(float(left) - float(right)) <= tolerance
        except (TypeError, ValueError):
            return False
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            tolerance_equal(left[key], right[key], tolerance) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            tolerance_equal(a, b, tolerance) for a, b in zip(left, right))
    return left == right


def make_temp_root(prefix: str = "examprep-bench-") -> tempfile.TemporaryDirectory[str]:
    """A temporary root that the caller must close (or use as a context manager)."""
    return tempfile.TemporaryDirectory(prefix=prefix)


def ensure_repo_importable() -> None:
    """Make ``python -m benchmarks.run_all`` work from any cwd inside the repo."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
