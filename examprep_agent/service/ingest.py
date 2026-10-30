# -*- coding: utf-8 -*-
"""Parallel ingestion pipeline: extract → chapters → figures → index chunks.

Choice of concurrency model: **threads**, not asyncio or processes.

* The extractors are blocking stdlib/third-party calls (``zipfile``, XML,
  optional ``pypdfium2``).  Wrapping them in asyncio would only add an executor
  underneath; a thread pool expresses the parallelism directly.
* A process pool would require pickling every ``Source``/``Chapter`` and would
  make the optional PDF backend's handles cross process boundaries; thread
  workers share the loaded modules and write disjoint output files.

Every stage is deterministic *given file order*: workers compute independent
per-file/per-chapter pieces and the main thread merges them in the original
order, so ``workers=1`` and ``workers=N`` produce byte-identical chapters,
question banks (extraction is upstream of questions) and indexes.  A failure
in one worker is captured as a ``Source.error`` (or a ``figures_failed``
warning) and never aborts the batch.

Progress is appended as JSONL to ``<workspace>/logs/ingest-YYYY-MM-DD.jsonl``
(one event per file plus start/done), so a slow or failing file is observable
without parsing stdout.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence, TypeVar

from ..security.audit import redact_secrets
from ..tools import chapters as chmod
from ..tools import extract, figures as figmod
from ..tools import index as idx

T = TypeVar("T")

DEFAULT_WORKERS = min(8, max(1, os.cpu_count() or 1))


def resolve_workers(workers: int | None = None) -> int:
    """Explicit value → ``EXAMPREP_INGEST_WORKERS`` → CPU-based default."""
    if workers is not None:
        return max(1, int(workers))
    env = os.environ.get("EXAMPREP_INGEST_WORKERS", "").strip()
    if env.isdigit():
        return max(1, int(env))
    return DEFAULT_WORKERS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class IngestProgress:
    """Append-only JSONL progress log for one ingestion run."""

    def __init__(self, path: str | None) -> None:
        self.path = os.path.abspath(path) if path else None

    def emit(self, event: str, **fields: Any) -> None:
        """Append one redacted event; a logging failure never stops ingestion."""
        if not self.path:
            return
        payload: dict[str, Any] = {"ts": _utc_now(), "event": event}
        payload.update(redact_secrets(fields))
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


def progress_path(workspace: str, day: str | None = None) -> str:
    """Daily ingestion progress file inside the workspace (internal path)."""
    stamp = day or datetime.now(timezone.utc).date().isoformat()
    return os.path.join(os.path.abspath(workspace), "logs", "ingest-%s.jsonl" % stamp)


def _run_parallel(items: Sequence[T], fn: Callable[[T], Any], workers: int) -> list[Any]:
    """Map ``fn`` over ``items`` preserving input order (serial when workers=1)."""
    if workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    results: list[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="examprep-ingest") as pool:
        futures = {pool.submit(fn, item): index for index, item in enumerate(items)}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def extract_sources(
    materials: str,
    exclude_dirs: Any = (),
    *,
    workers: int | None = None,
    progress: IngestProgress | None = None,
) -> list[extract.Source]:
    """Extract every supported file, in natural file order, with per-file isolation."""
    paths = extract.list_files(materials, exclude_dirs=exclude_dirs)
    count = resolve_workers(workers)
    progress = progress or IngestProgress(None)
    progress.emit("ingest_start", materials=materials, files=len(paths), workers=count)

    def one(path: str) -> extract.Source:
        started = time.perf_counter()
        try:
            source = extract.extract_source(materials, path)
        except Exception as exc:  # extract_source is defensive; this is the second net
            rel = os.path.relpath(path, materials).replace("\\", "/")
            source = extract.Source(path, rel, "other")
            source.error = "%s: %s" % (type(exc).__name__, exc)
        progress.emit(
            "file_extracted",
            file=source.rel,
            ok=source.error is None,
            error=source.error,
            pages=len(source.pages),
            chars=len(source.text),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return source

    started = time.perf_counter()
    sources = _run_parallel(paths, one, count)
    errors = sum(1 for source in sources if source.error)
    progress.emit(
        "ingest_done",
        files=len(sources),
        errors=errors,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    return sources


def build_chapters_parallel(
    sources: Sequence[extract.Source],
    *,
    workers: int | None = None,
    progress: IngestProgress | None = None,
) -> list[chmod.Chapter]:
    """Split sources into chapters with per-file planning in a thread pool.

    A planner failure is isolated to that source: a ``chapter_plan_failed``
    event is recorded, the source is skipped and every other file still
    becomes a chapter.
    """
    sources = list(sources)
    progress = progress or IngestProgress(None)

    def plan(source: extract.Source) -> dict[str, Any] | None:
        try:
            return chmod.source_plan(source)
        except Exception as exc:  # one bad source must not abort the batch
            message = "chapter_plan_failed: %s: %s" % (type(exc).__name__, exc)
            source.warnings.append(message)
            progress.emit("chapter_plan_failed", file=source.rel, error=message)
            return None

    plans = _run_parallel(sources, plan, resolve_workers(workers))
    return chmod.build_chapters(sources, plans=plans)


def extract_figures_parallel(
    materials: str,
    sources: Sequence[extract.Source],
    ws: str,
    *,
    workers: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, set[int]], dict[str, dict[int, figmod.PdfPageInfo]]]:
    """Extract figures per source concurrently; results stay in source order."""
    sources = list(sources)
    out_dir = os.path.join(ws, figmod.FIG_DIR)
    os.makedirs(out_dir, exist_ok=True)
    figmod.clean_figure_dir(materials, ws)
    pdfium_ok = figmod.has_pdfium()

    def one(source: extract.Source) -> tuple[list[dict[str, Any]], dict[str, set[int]],
                                             dict[str, dict[int, figmod.PdfPageInfo]], str | None]:
        try:
            recs, scans, infos = figmod.extract_source_figures(source, out_dir, pdfium_ok)
            return recs, scans, infos, None
        except Exception as exc:  # one bad file must not stop setup
            return [], {}, {}, "figures_failed: %s" % exc

    results = _run_parallel(sources, one, resolve_workers(workers))
    records: list[dict[str, Any]] = []
    scan_pages: dict[str, set[int]] = {}
    pdf_infos: dict[str, dict[int, figmod.PdfPageInfo]] = {}
    owners: dict[str, str] = {}
    for source, (recs, scans, infos, warning) in zip(sources, results):
        if warning:
            source.warnings.append(warning)
        kept: list[dict[str, Any]] = []
        for record in recs:
            path = str(record.get("path") or "")
            owner = owners.get(path)
            if owner is not None and owner != source.rel:
                # Defence in depth: ``safe_stem`` makes this practically
                # impossible, but a collision must never silently overwrite
                # another source's crop or make workers=1/workers=N diverge.
                source.warnings.append(
                    "figures_collision: %s already produced by %s" % (path, owner))
                continue
            owners[path] = source.rel
            kept.append(record)
        records.extend(kept)
        scan_pages.update(scans)
        pdf_infos.update(infos)
    return records, scan_pages, pdf_infos


def build_chunks_parallel(
    chapters: Iterable[chmod.Chapter],
    *,
    workers: int | None = None,
    progress: IngestProgress | None = None,
) -> list[dict[str, Any]]:
    """Build BM25 chunks per chapter concurrently, merged in chapter order.

    A failing chapter contributes no chunks (the rest of the index is still
    built) and is recorded as a ``chunks_failed`` progress event.
    """
    chapters = list(chapters)
    progress = progress or IngestProgress(None)

    def chunks(chapter: chmod.Chapter) -> list[dict[str, Any]]:
        try:
            return idx.chunks_for_chapter(chapter)
        except Exception as exc:  # one bad chapter must not abort the batch
            message = "chunks_failed: %s: %s" % (type(exc).__name__, exc)
            progress.emit("chunks_failed", chapter=chapter.number, error=message)
            return []

    per_chapter = _run_parallel(chapters, chunks, resolve_workers(workers))
    return idx.build_chunks(chapters, per_chapter=per_chapter)


def question_bank(
    sources: Sequence[extract.Source],
    chapters: Sequence[chmod.Chapter],
    materials: str,
    exclude_dirs: Any = (),
) -> list[dict[str, Any]]:
    """Deterministic question extraction plus prebuilt-bank import.

    Question extraction is kept sequential on purpose: solution files are
    paired with question files *across* sources (``hw2`` ↔ ``hw2solutions``),
    so the unit of work is not a single file.  Everything around it (text
    extraction, chapter splitting, index building) is parallel.
    """
    from ..tools import questions as qmod

    bank = qmod.extract_questions(sources, chapters)
    imported = qmod.import_question_banks(materials, chapters, exclude_dirs=exclude_dirs)
    if imported:
        existing_ids = {q["id"] for q in bank}
        for item in imported:
            if item["id"] in existing_ids:
                item["id"] = "bank_" + item["id"]
            existing_ids.add(item["id"])
        bank += imported
    return bank
