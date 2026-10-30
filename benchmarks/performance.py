# -*- coding: utf-8 -*-
"""Small, machine-specific performance probes (not a scored benchmark layer).

Reuses the P2 ingestion methodology: identical work, serial (``workers=1``) vs
threaded (``workers = CPU count``), median over repeated runs, same business
products.  Absolute latency is machine-specific and reported with the current
container description.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .common import (
    copy_sample_course,
    machine_info,
    median,
    percentile,
    run_cli,
)

CORPUS_FILES = 48
CORPUS_PAGES = 4
CORPUS_REPEATS = 3


def _generate_corpus(root: Path) -> tuple[Path, int, int]:
    """Deterministic multi-page markdown corpus for ingestion timing."""
    corpus = root / "corpus"
    corpus.mkdir(parents=True)
    for index in range(CORPUS_FILES):
        pages = []
        for page in range(CORPUS_PAGES):
            lines = ["# Course %02d page %d" % (index + 1, page + 1), ""]
            for paragraph in range(6):
                lines.append(
                    "topic%02d_page%d_paragraph%d：这是第 %d 个文件第 %d 页的第 %d 段，"
                    "用于确定性地测量摄取吞吐；知识点编号 kp-%02d-%d-%d。"
                    % (index + 1, page + 1, paragraph + 1, index + 1, page + 1,
                       paragraph + 1, index + 1, page + 1, paragraph + 1)
                )
                lines.append("")
            pages.append("\n".join(lines))
        (corpus / ("course_%02d.md" % (index + 1))).write_text(
            "\f".join(pages), encoding="utf-8")
    return corpus, CORPUS_FILES, CORPUS_FILES * CORPUS_PAGES


def _time_setup(materials: Path, workspace: Path, workers: int) -> float:
    from examprep_agent.service.models import SetupRequest
    from examprep_agent.service.study import StudyService

    started = time.perf_counter()
    StudyService(pointer=str(workspace.parent / "pointer")).setup(
        SetupRequest(materials=str(materials), workspace=str(workspace),
                     lang="zh", workers=workers))
    return time.perf_counter() - started


def _ingest_probe(root: Path, workers: int, repeats: int) -> dict[str, Any]:
    corpus, files, pages = _generate_corpus(root / ("corpus-w%d" % workers))
    times: list[float] = []
    for repeat in range(repeats):
        run_root = root / ("run-w%d-%d" % (workers, repeat))
        materials = run_root / "materials"
        shutil.copytree(corpus, materials)
        elapsed = _time_setup(materials, run_root / "workspace", workers)
        times.append(elapsed)
    elapsed_median = median(times) or 0.0
    return {
        "workers": workers,
        "repeats": repeats,
        "files": files,
        "pages": pages,
        "elapsed_seconds": [round(value, 4) for value in times],
        "median_seconds": round(elapsed_median, 4),
        "files_per_minute": round(files * 60.0 / elapsed_median, 2) if elapsed_median else None,
        "pages_per_minute": round(pages * 60.0 / elapsed_median, 2) if elapsed_median else None,
    }


def _service_latency(root: Path, reps: int = 20) -> dict[str, Any]:
    from examprep_agent.service.models import QuizRequest, SetupRequest
    from examprep_agent.service.study import StudyService

    course = copy_sample_course(root / "latency-course")
    workspace = str(root / "latency-workspace")
    service = StudyService(pointer=str(root / "pointer"))
    service.setup(SetupRequest(materials=str(course), workspace=workspace,
                               lang="zh", days=30, workers=1))

    def timed(fn: Callable[[], Any], reps: int = reps) -> list[float]:
        samples: list[float] = []
        for _ in range(reps):
            started = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - started) * 1000.0)
        return samples

    status = timed(lambda: service.status(workspace))
    ask = timed(lambda: service.ask(workspace, "循环队列 判满", k=5))
    quiz = timed(lambda: service.quiz(QuizRequest(workspace=workspace, n=3)))
    cli_status = timed(lambda: run_cli("status", "--workspace", workspace), reps=10)
    return {
        "repeats": reps,
        "status_ms": {"median": median(status), "p95": percentile(status, 95)},
        "ask_ms": {"median": median(ask), "p95": percentile(ask, 95)},
        "quiz_ms": {"median": median(quiz), "p95": percentile(quiz, 95)},
        "cli_status_ms": {"median": median(cli_status), "p95": percentile(cli_status, 95)},
        "note": "in-process service calls; CLI status includes rendering and audit, excludes interpreter startup",
    }


def run(root: str | Path) -> dict[str, Any]:
    """Run the performance probes and return their results section."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cpus = max(1, os.cpu_count() or 1)
    parallel_workers = min(8, cpus)
    serial = _ingest_probe(root / "ingest", 1, CORPUS_REPEATS)
    parallel = _ingest_probe(root / "ingest", parallel_workers, CORPUS_REPEATS)
    speedup = (serial["median_seconds"] / parallel["median_seconds"]
               if parallel["median_seconds"] else None)
    latency = _service_latency(root / "latency")
    return {
        "layer": "Performance",
        "deterministic": False,
        "machine": machine_info(),
        "ingest": {
            "corpus": {"files": serial["files"], "pages": serial["pages"],
                       "repeats": CORPUS_REPEATS},
            "serial": serial,
            "parallel": parallel,
            "parallel_workers": parallel_workers,
            "speedup_serial_over_parallel": round(speedup, 4) if speedup else None,
            "method": "same corpus copied per run; median wall time of full StudyService.setup",
        },
        "commands": latency,
        "note": "timing varies by machine; the report names the current container and does not claim CI reproducibility",
    }
