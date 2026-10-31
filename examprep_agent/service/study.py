# -*- coding: utf-8 -*-
"""Core study service: one implementation behind CLI, HTTP and MCP.

Every method takes/returns structured models and owns the business logic that
used to live in ``cli.py``: workspace resolution, the ingestion pipeline,
planner decisions, question selection, answer recording, graph/permission
administration and cost aggregation.  Presentation (bilingual wording, terminal
layout) stays in the command-line layer; transports never duplicate logic.

Expected failures (missing workspace, unknown question, no quiz questions, no
material match) are returned as results carrying the CLI exit code, or raised
as :class:`ServiceError`; both map to the same semantics over HTTP.
"""
from __future__ import annotations

import copy
import os
import re
import time
from contextlib import contextmanager
from datetime import date
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..core.provider import ModelProvider
from ..core.session import Session
from ..learning.graph import GRAPH_FILE, GraphNode, build_graph, kp_for_chapter
from ..learning.modes import LONGTERM_THRESHOLD_DAYS, StudyPolicy, select_policy
from ..learning.planner import order_chapters, review_plan
from ..security.atomicio import atomic_write_text
from ..security.audit import redact_text
from ..security.commands import DangerousCommandScanner
from ..security.locking import WorkspaceBusyError, workspace_lock
from ..security.paths import validate_grant_target, validate_workspace
from ..security.risk import RiskLevel
from ..security.trash import TrashManager
from ..tools import chapters as chmod
from ..tools import figures as figmod
from ..tools import guard as gd
from ..tools import index as idx
from ..tools import questions as qmod
from ..tools import state as st
from ..tools.text import is_mostly_cjk, pack, shorten
from . import ingest
from .costs import aggregate_costs
from .errors import EXIT_NO_MATERIAL, EXIT_NO_QUESTIONS, EXIT_USAGE, ServiceError, UnknownChapter, UnknownQuestion, WorkspaceNotFound
from .models import (
    AnswerResult,
    AskHit,
    AskResult,
    ChapterStatus,
    ChapterSummary,
    CheckResult,
    CheatsheetRequest,
    CheatsheetResult,
    CommandCost,
    CostsResult,
    DoneRequest,
    DoneResult,
    DueReviewView,
    FigureRecord,
    FooterInfo,
    GotoRequest,
    GotoResult,
    GradeRequest,
    GradeResult,
    GraphNodeView,
    GraphRequest,
    GraphResult,
    MistakeItem,
    MistakesResult,
    NextResult,
    NoteRequest,
    NoteResult,
    PermissionRequest,
    PermissionView,
    PermissionsResult,
    PlanDay,
    PlanResult,
    PrereqGapView,
    QuestionRecord,
    QuizRequest,
    QuizResult,
    ReadError,
    SetupRequest,
    SetupResult,
    StatusResult,
)
from .subagent import grading_subagent

CHAPTERS_FILE = "chapters.json"
BANK_FILE = "quiz_bank.json"
FIGURES_FILE = "figures.json"
DEFAULT_POINTER = os.path.join(os.path.expanduser("~"), ".examprep-agent", "last_workspace")

PROFILES = {
    "small": {"slice": {"zh": 1800, "en": 3000}, "examples": 3, "quiz_n": 1},
    "standard": {"slice": {"zh": 3000, "en": 5000}, "examples": 8, "quiz_n": 3},
    "large": {"slice": {"zh": 8000, "en": 12000}, "examples": 15, "quiz_n": 5},
}


# ------------------------------------------------------------------ content helpers
def chapter_parts(ch: chmod.Chapter, slice_chars: int) -> list[list[tuple[str, str]]]:
    """Stable split of a chapter into teachable pieces: [[(anchor, text), ...], ...]."""
    segments: list[tuple[str, str]] = []
    for rel, page, text in ch.blocks:
        for piece in pack(text, slice_chars):
            segments.append(("%s p.%s" % (rel, page), piece))
    parts: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    size = 0
    for seg in segments:
        if cur and size + len(seg[1]) > slice_chars:
            parts.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += len(seg[1])
    if cur:
        parts.append(cur)
    return parts


def render_part(part: Sequence[tuple[str, str]]) -> str:
    """Render a chapter part with ``[file p.N]`` anchors."""
    out: list[str] = []
    last_anchor: str | None = None
    for anchor, text in part:
        if anchor != last_anchor:
            out.append("[%s]" % anchor)
            last_anchor = anchor
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip()


def part_pages(part: Sequence[tuple[str, str]]) -> list[tuple[str, int]]:
    pages: list[tuple[str, int]] = []
    for anchor, _ in part:
        match = re.match(r"(.+) p\.(\d+)$", anchor)
        if match and (match.group(1), int(match.group(2))) not in pages:
            pages.append((match.group(1), int(match.group(2))))
    return pages


def figures_for_pages(figs: Sequence[Mapping[str, Any]],
                      pages: Sequence[tuple[str, int]]) -> list[FigureRecord]:
    wanted = set(pages)
    out = []
    for item in figs:
        if item.get("kind") == "figure" and (item.get("file"), item.get("page")) in wanted:
            out.append(FigureRecord(**{key: item[key] for key in FigureRecord.model_fields if key in item}))
    return out


def days_for_study(state: Mapping[str, Any]) -> int | None:
    """Days available for new material; the last day is reserved for review."""
    days = st.days_left(state)
    if days is None:
        return None
    return days - 1 if days >= 2 else 1


def daily_groups(state: Mapping[str, Any],
                 order: Sequence[int] | None = None) -> list[list[dict[str, Any]]]:
    """Split remaining chapters over remaining study days (order-aware)."""
    todo = [c for c in state["chapters"] if c["status"] == "todo"]
    cur = st.chapter(state)
    if order is None and cur and cur["status"] == "todo":
        todo = [cur] + [c for c in todo if c["n"] != cur["n"]]
    elif order is not None:
        rank = {n: index for index, n in enumerate(order)}
        todo.sort(key=lambda c: (rank.get(c["n"], 10_000), c["n"]))
    days = days_for_study(state)
    if not todo or not days:
        return [todo] if todo else []
    days = min(days, len(todo))
    base, extra = divmod(len(todo), days)
    groups: list[list[dict[str, Any]]] = []
    index = 0
    for day_index in range(days):
        size = base + (1 if day_index < extra else 0)
        groups.append(todo[index:index + size])
        index += size
    return groups


def _advance_to_next_todo(state: dict[str, Any],
                          order: Sequence[int] | None = None) -> dict[str, Any] | None:
    todo = [c for c in state["chapters"] if c["status"] == "todo"]
    if not todo:
        return None
    if order:
        rank = {n: index for index, n in enumerate(order)}
        todo.sort(key=lambda c: (rank.get(c["n"], 10_000), c["n"]))
    state["current"] = todo[0]["n"]
    return todo[0]


def _sanitize_title(title: str, number: int, lang: str) -> str:
    if gd.is_suspicious(title) or gd.neutralize(title) != title:
        return ("第 %d 章" if lang == "zh" else "Chapter %d") % number
    if len(title) > 80:
        return shorten(title, 80)
    return title


def question_counts(bank: Sequence[Mapping[str, Any]]) -> dict[int | None, tuple[int, int]]:
    """{chapter: (questions, past-exam questions)}."""
    counts: dict[int | None, tuple[int, int]] = {}
    for question in bank:
        n, exam = counts.get(question.get("chapter"), (0, 0))
        counts[question.get("chapter")] = (
            n + 1,
            exam + (1 if (question.get("source") or {}).get("kind") == "exam" else 0),
        )
    return counts


def focus_chapters(state: Mapping[str, Any], bank: Sequence[Mapping[str, Any]],
                   groups: Sequence[Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """When a day holds more than two chapters, keep the ones past exams ask most."""
    days = days_for_study(state)
    todo = [c for group in groups for c in group]
    if not days or len(todo) <= 2 * days:
        return []
    counts = question_counts(bank)
    ranked = sorted(
        (c for c in todo if counts.get(c["n"], (0, 0))[1]),
        key=lambda c: -counts[c["n"]][1],
    )
    return ranked[: 2 * days]


# ------------------------------------------------------------------ figure crop helpers
def _segments_for(
    infos: dict[int, figmod.PdfPageInfo],
    heads: list[tuple[int, str, str]],
    index: int,
) -> list[tuple[int, float, float]]:
    """Page regions (page, top, bottom) covered by block i, given all block heads in order."""
    page, head = heads[index][0], heads[index][1]
    info = infos.get(page)
    top = figmod.find_text_top(info, head) if info else None
    if top is None:
        return []
    nxt = heads[index + 1] if index + 1 < len(heads) else None
    if nxt and nxt[0] == page:
        nt = figmod.find_text_top(infos[page], nxt[1])
        return [(page, top, nt if nt is not None else 24)]
    segs: list[tuple[int, float, float]] = [(page, top, 24)]
    if info and info.scan:
        return segs
    last = nxt[0] if nxt else min(page + 3, max(infos))
    for p in range(page + 1, last):
        if p in infos:
            segs.append((p, infos[p].height - 24, 24))
    if nxt and nxt[0] in infos:
        nt = figmod.find_text_top(infos[nxt[0]], nxt[1])
        if nt is not None:
            segs.append((nxt[0], infos[nxt[0]].height - 24, nt))
    return segs


def attach_question_figures(
    materials: str,
    ws: str,
    bank: list[dict[str, Any]],
    pdf_infos: dict[str, dict[int, figmod.PdfPageInfo]],
    sources: Sequence[Any],
) -> list[dict[str, Any]]:
    """Crop the printed region of each question/answer that contains a picture."""
    by_rel = {source.rel: source for source in sources}
    head_cache: dict[str, list[tuple[int, str, str]]] = {}

    def heads_of(rel: str) -> list[tuple[int, str, str]]:
        if rel not in head_cache:
            head_cache[rel] = qmod.heads(by_rel[rel]) if rel in by_rel else []
        return head_cache[rel]

    records: list[dict[str, Any]] = []
    for item in bank:
        for side, key in (("source", "figures"), ("answer_source", "answer_figures")):
            ref = item.get(side)
            item[key] = []
            if not ref or ref["file"] not in pdf_infos:
                continue
            heads = heads_of(ref["file"])
            position = next((k for k, head in enumerate(heads)
                             if head[0] == ref["page"] and head[1] == ref.get("head")), None)
            if position is None:
                continue
            segments = _segments_for(pdf_infos[ref["file"]], heads, position)
            label = item["id"] if side == "source" else item["id"] + "_ans"
            paths = figmod.crop_blocks(materials, ws, ref["file"], pdf_infos[ref["file"]],
                                       segments, figmod.safe_stem(ref["file"]), label)
            item[key] = paths
            for path in paths:
                records.append({"file": ref["file"], "page": ref["page"], "path": path,
                                "kind": "question" if side == "source" else "answer",
                                "qid": item["id"], "box": None})
    return records


# ------------------------------------------------------------------ service
class StudyService:
    """Stateless facade over one workspace per call (transport-safe)."""

    def __init__(self, pointer: str | None = None, cwd: str | None = None) -> None:
        self.pointer = pointer if pointer is not None else DEFAULT_POINTER
        self.cwd = cwd or os.getcwd()

    # -------------------------------------------------------------- workspaces
    def resolve_workspace(self, explicit: str | None = None) -> str | None:
        """Find a workspace: explicit flag, env vars, ./exam-cram, then the pointer."""
        for candidate in (explicit,
                          os.environ.get("EXAMPREP_WORKSPACE"),
                          os.environ.get("EXAM_CRAM_WORKSPACE")):
            if candidate and os.path.exists(st.path(candidate)):
                return os.path.abspath(candidate)
        local = os.path.join(self.cwd, "exam-cram")
        if os.path.exists(st.path(local)):
            return os.path.abspath(local)
        if os.path.exists(self.pointer):
            try:
                with open(self.pointer, encoding="utf-8") as fh:
                    candidate = fh.read().strip()
            except OSError:
                candidate = ""
            if candidate and os.path.exists(st.path(candidate)):
                return os.path.abspath(candidate)
        return None

    def require_workspace(self, explicit: str | None = None) -> str:
        """Resolve or raise :class:`WorkspaceNotFound`."""
        workspace = self.resolve_workspace(explicit)
        if workspace is None:
            raise WorkspaceNotFound()
        return workspace

    def remember_workspace(self, workspace: str) -> None:
        """Best-effort pointer to the most recently used workspace."""
        try:
            os.makedirs(os.path.dirname(self.pointer), exist_ok=True)
            with open(self.pointer, "w", encoding="utf-8") as fh:
                fh.write(workspace)
        except OSError:
            pass

    def _load_path(self, path: str) -> Session:
        try:
            return Session.load(path)
        except FileNotFoundError as exc:
            raise WorkspaceNotFound() from exc
        except (ValueError, TypeError, OSError) as exc:
            raise ServiceError(
                "workspace state is unreadable (%s): %s" % (path, exc),
                code="workspace_unreadable",
                exit_code=EXIT_USAGE,
                details={"workspace": path},
            ) from exc

    def load(self, workspace: str | None = None) -> Session:
        """Load a study session or raise a structured service error."""
        return self._load_path(self.require_workspace(workspace))

    @contextmanager
    def _locked_workspace(self, workspace: str) -> Iterator[None]:
        """Exclusive per-workspace lock for one read-modify-write transition."""
        try:
            with workspace_lock(workspace):
                yield
        except WorkspaceBusyError as exc:
            raise ServiceError(str(exc), code="workspace_busy",
                               exit_code=EXIT_USAGE) from exc

    @contextmanager
    def _locked_session(self, workspace: str | None = None) -> Iterator[Session]:
        """Lock, then load: the lock is held until the caller's save finishes."""
        path = self.require_workspace(workspace)
        with self._locked_workspace(path):
            yield self._load_path(path)

    def _audit_tool(self, session: Session, tool: str | None, access: str) -> None:
        """Record a read/write performed on behalf of an external tool surface.

        CLI and HTTP use trusted host writes; MCP passes a tool name so every
        MCP call also appears in the permission audit (same gate, no throttle:
        these are semantic study operations, not arbitrary file writes).
        """
        if not tool:
            return
        level = RiskLevel.L0 if access == "read" else RiskLevel.L1
        decision = session.gate.check(tool, level, {}, access=access, count_throttle=False)
        if not decision.allowed:
            raise ServiceError(decision.reason, code=decision.code,
                               exit_code=EXIT_USAGE, details=decision.to_dict())

    # -------------------------------------------------------------- footer
    @staticmethod
    def footer_info(session: Session) -> FooterInfo:
        """Data behind the deterministic ``📍`` guidance line."""
        state = session.state
        total = len(state["chapters"])
        done = sum(1 for c in state["chapters"] if c["status"] in ("done", "verified"))
        chapter = st.chapter(state)
        right = asked = 0
        part = parts = 0
        title = ""
        chapter_n = None
        if chapter is not None:
            chapter_n = int(chapter["n"])
            title = str(chapter["title"])
            part = int(chapter["part"])
            parts = int(chapter["parts"])
            right, asked = st.chapter_results(state, chapter["n"])
        return FooterInfo(
            current_chapter=chapter_n if chapter is not None and chapter["status"] == "todo" else None,
            current_title=title,
            current_part=part,
            current_parts=parts,
            chapter_right=right,
            chapter_asked=asked,
            mastery=session.mastery(chapter_n) if chapter_n is not None else 0.0,
            done=done,
            total=total,
            open_mistakes=len(st.open_mistakes(state)),
            days_left=st.days_left(state),
        )

    # -------------------------------------------------------------- content loaders
    @staticmethod
    def load_chapters(ws: str) -> dict[int, chmod.Chapter]:
        """Load ``chapters.json`` into chapter objects."""
        path = os.path.join(ws, CHAPTERS_FILE)
        data = _read_json(path, [])
        return {item["n"]: chmod.Chapter.from_dict(item) for item in data}

    @staticmethod
    def load_bank(ws: str) -> list[dict[str, Any]]:
        """Load the quiz bank."""
        return _read_json(os.path.join(ws, BANK_FILE), [])

    @staticmethod
    def load_figures(ws: str) -> list[dict[str, Any]]:
        """Load the figure index."""
        return _read_json(os.path.join(ws, FIGURES_FILE), [])

    @staticmethod
    def find_question(bank: Sequence[Mapping[str, Any]], qid: str) -> dict[str, Any] | None:
        """Find one question by id."""
        return next((dict(q) for q in bank if q.get("id") == qid), None)

    # -------------------------------------------------------------- setup
    def setup(self, request: SetupRequest, *, audit_tool: str | None = None) -> SetupResult:
        """Build a workspace from a materials folder (parallel ingestion)."""
        started = time.time()
        materials = os.path.abspath(request.materials)
        if not os.path.isdir(materials):
            raise ServiceError("materials folder not found: %s" % materials,
                               code="materials_not_found", exit_code=EXIT_USAGE)
        ws = os.path.abspath(request.workspace or os.path.join(materials, "exam-cram"))
        try:
            validate_workspace(ws, materials)
        except ValueError as exc:
            raise ServiceError("unsafe workspace: %s" % exc,
                               code="unsafe_workspace", exit_code=EXIT_USAGE) from exc
        os.makedirs(os.path.join(ws, "chapters"), exist_ok=True)
        # A setup is one large read-modify-write transition: serialise it per
        # workspace so it cannot interleave with a concurrent answer/next.
        with self._locked_workspace(ws):
            return self._setup_locked(request, materials, ws, started, audit_tool)

    def _setup_locked(self, request: SetupRequest, materials: str, ws: str,
                      started: float, audit_tool: str | None) -> SetupResult:
        workers = ingest.resolve_workers(request.workers)
        progress = ingest.IngestProgress(ingest.progress_path(ws))

        # The workspace may live inside the materials folder (documented
        # --workspace usage); never re-ingest its own generated files.
        sources = ingest.extract_sources(materials, exclude_dirs=[ws], workers=workers, progress=progress)
        fig_records, scan_pages, pdf_infos = ingest.extract_figures_parallel(
            materials, sources, ws, workers=workers)
        scanned = 0
        for source in sources:
            scans = scan_pages.get(source.rel, set())
            for page in source.pages:
                page.scan = page.number in scans
            scanned += len(scans)
        chapters = ingest.build_chapters_parallel(sources, workers=workers, progress=progress)
        all_text = "\n".join(chapter.text for chapter in chapters)
        lang = request.lang or ("zh" if is_mostly_cjk(all_text) else "en")
        for chapter in chapters:
            chapter.title = _sanitize_title(chapter.title, chapter.number, lang)

        previous = st.load(ws)
        keep_old = bool(previous and previous.get("version") == 5 and not request.fresh)
        profile = request.profile or (previous.get("profile") if keep_old else None) or "standard"
        slice_chars = request.slice or PROFILES[profile]["slice"][lang]
        bank = ingest.question_bank(sources, chapters, materials, exclude_dirs=[ws])
        fig_records += attach_question_figures(materials, ws, bank, pdf_infos, sources)
        chunks = ingest.build_chunks_parallel(chapters, workers=workers, progress=progress)

        if request.fresh:
            trash = TrashManager(ws, materials)
            for name in ("memory", GRAPH_FILE, "session.json"):
                old = os.path.join(ws, name)
                if os.path.exists(old):
                    try:
                        trash.delete(old, reason="setup --fresh")
                    except (OSError, ValueError):
                        pass

        scanner = DangerousCommandScanner()
        findings = []
        for source in sources:
            if source.error or not source.text:
                continue
            findings.extend(scanner.scan(source.text, source=source.rel))
        if bank:
            findings.extend(scanner.scan(_json_dumps(bank), source="quiz_bank.json"))
        security_scan = {
            "scanned": True,
            "count": len(findings),
            "findings": [finding.to_dict() for finding in findings],
        }

        graph = build_graph(chapters)
        if keep_old:
            graph.apply_edits(previous.get("graph_edits"))
        graph.save(os.path.join(ws, GRAPH_FILE))

        trash = TrashManager(ws, materials)
        for old in os.listdir(os.path.join(ws, "chapters")):
            if old.startswith("ch") and old.endswith(".md"):
                try:
                    trash.delete(os.path.join(ws, "chapters", old), reason="setup regeneration")
                except (OSError, ValueError):
                    continue
        for chapter in chapters:
            safe = re.sub(r"[^\w\-]+", "_", chapter.title)[:24].strip("_")
            body = "# %d. %s\n\n" % (chapter.number, chapter.title)
            body += "\n\n".join(render_part(part) for part in chapter_parts(chapter, slice_chars)) + "\n"
            for filename in ("ch%02d_%s.md" % (chapter.number, safe) if safe else "",
                             "ch%02d.md" % chapter.number):
                if not filename:
                    continue
                try:
                    with open(os.path.join(ws, "chapters", filename), "w", encoding="utf-8") as fh:
                        fh.write(body)
                    break
                except OSError:
                    continue

        snapshot_root = os.path.join(ws, ".snapshots")
        _write_json(os.path.join(ws, CHAPTERS_FILE), [c.to_dict() for c in chapters], snapshot_root)
        _write_json(os.path.join(ws, BANK_FILE), bank, snapshot_root)
        _write_json(os.path.join(ws, FIGURES_FILE), fig_records, snapshot_root)
        idx.save_index(ws, chunks)

        course = request.name or os.path.basename(materials.rstrip("\\/")) or "course"
        chapter_summaries = [{
            "n": chapter.number,
            "title": chapter.title,
            "sources": chapter.sources,
            "parts": len(chapter_parts(chapter, slice_chars)),
            "questions": sum(1 for question in bank if question["chapter"] == chapter.number),
        } for chapter in chapters]
        exam_date = _exam_date(request)
        state = st.new_state(course, lang, exam_date, materials, chapter_summaries,
                             slice_chars, profile)
        if keep_old:
            keep = {c["n"]: c for c in previous["chapters"]}
            for chapter in state["chapters"]:
                if chapter["n"] in keep:
                    chapter["status"] = keep[chapter["n"]]["status"]
                    chapter["part"] = min(keep[chapter["n"]]["part"], chapter["parts"])
            state["history"] = previous["history"]
            state["mistakes"] = previous["mistakes"]
            state["notes"] = previous["notes"]
            state["current"] = previous["current"] if st.chapter(state, previous["current"]) else state["current"]
            state["created"] = previous["created"]
            for key in ("learner", "reviews"):
                if key in previous:
                    state[key] = previous[key]
            state["graph_edits"] = previous.get("graph_edits") or {"add": [], "remove": []}
            if exam_date is None:
                state["exam_date"] = previous.get("exam_date")
        if request.start:
            state["current"] = request.start
        state["security_scan"] = security_scan

        days = st.days_left(state)
        policy = select_policy(days, request.mode)
        state["mode"] = policy.name
        state["policy"] = policy.to_dict()
        reason_code, reason_params = _mode_reason(request.mode, days, policy)
        # Persist the same human-readable field P1 wrote (state schema stability)
        # alongside the structured code/params returned to transports.
        state["mode_reason"] = _mode_reason_text(reason_code, reason_params, lang, policy.name)
        session = Session.create(ws, state, policy.name)
        session.graph = graph
        if keep_old:
            previous_checkpoint = Session._read_checkpoint(ws)
            session.context.summary = previous_checkpoint.context_summary
            session.counters.update(previous_checkpoint.counters)
            if previous_checkpoint.branch:
                session.branch = previous_checkpoint.branch
        try:
            session.memory.record_session_event(
                "setup", course=course, chapters=len(chapters), mode=policy.name,
                security_findings=len(findings),
            )
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            session.log_memory_error("episodic", exc)
        session.audit.log(
            "material_scan", materials=materials, findings=len(findings),
            by_kind={kind: sum(1 for item in findings if item.kind == kind)
                     for kind in sorted({item.kind for item in findings})},
        )
        self._audit_tool(session, audit_tool, "write")
        session.save()
        session.save_graph()
        self.remember_workspace(ws)

        kinds: dict[str, int] = {}
        for source in sources:
            kinds[source.kind] = kinds.get(source.kind, 0) + 1
        pdf_missing = [s.rel for s in sources if s.error == "pdf_support_missing"]
        read_errors = [ReadError(file=s.rel, error=str(s.error)) for s in sources
                       if s.error and s.error != "pdf_support_missing"]
        no_text = [s.rel for s in sources if "no_text" in s.warnings]
        figure_failures = ["%s: %s" % (s.rel, warning) for s in sources
                           for warning in s.warnings if warning.startswith("figures_failed")]
        injection_files = {
            s.rel: [str(page.number) for page in s.pages
                    if not s.error and gd.flagged_lines(page.text, limit=1)]
            for s in sources
            if s.pages and not s.error
        }
        injection_files = {name: pages for name, pages in injection_files.items() if pages}
        return SetupResult(
            workspace=ws,
            course=course,
            language=lang,
            mode=policy.name,
            mode_reason_code=reason_code,
            mode_reason_params=reason_params,
            exam_date=state.get("exam_date"),
            days_left=days,
            files=kinds,
            chapters=[ChapterSummary(**summary) for summary in chapter_summaries],
            questions=len(bank),
            answered=sum(1 for question in bank if question.get("answer")),
            figures=sum(1 for record in fig_records if record["kind"] == "figure"),
            question_figures=sum(1 for record in fig_records if record["kind"] in ("question", "answer")),
            scanned_pages=scanned,
            has_pdf_sources=any(s.rel.lower().endswith(".pdf") for s in sources),
            pdfium_available=figmod.has_pdfium(),
            pdf_missing=pdf_missing,
            read_errors=read_errors,
            no_text_files=no_text,
            figure_failures=figure_failures,
            injection_files=injection_files,
            security_findings=len(findings),
            elapsed_seconds=time.time() - started,
            footer=self.footer_info(session),
        )

    # -------------------------------------------------------------- status
    def status(self, workspace: str | None = None, *, audit_tool: str | None = None) -> StatusResult:
        """Progress panel data (chapters, mistakes, graph, review, daily budget)."""
        session = self.load(workspace)
        self._audit_tool(session, audit_tool, "read")
        state = session.state
        done = sum(1 for c in state["chapters"] if c["status"] in ("done", "verified"))
        current = st.chapter(state)
        chapters = []
        for chapter in state["chapters"]:
            right, asked = st.chapter_results(state, chapter["n"])
            chapters.append(ChapterStatus(
                n=int(chapter["n"]), title=chapter["title"], status=chapter["status"],
                part=int(chapter["part"]), parts=int(chapter["parts"]),
                right=right, asked=asked, mastery=session.mastery(chapter["n"]),
                current=bool(current and chapter["n"] == current["n"]),
            ))
        bank = self.load_bank(session.workspace)
        stats = session.graph_stats(bank)
        due = session.due_reviews(bank)
        counters = session.daily()
        groups = daily_groups(state)
        today = [c["n"] for c in groups[0]] if groups and groups[0] else []
        return StatusResult(
            workspace=session.workspace,
            course=state["course"],
            language=state["language"],
            mode=state.get("mode", "longterm"),
            exam_date=state.get("exam_date"),
            days_left=st.days_left(state),
            done=done,
            total=len(state["chapters"]),
            chapters=chapters,
            open_mistakes=len(st.open_mistakes(state)),
            confusions=len([n for n in state["notes"] if n["type"] == "confusion"]),
            graph=stats,
            due_count=len(due),
            retention=session.predicted_retention(),
            daily_new_used=len(counters["new_chapters"]),
            daily_new_limit=int(session.policy.daily_new_limit),
            today_chapters=today,
            footer=self.footer_info(session),
        )

    # -------------------------------------------------------------- plan
    def plan(self, workspace: str | None = None, *, days: int | None = None,
             exam_date: str | None = None, audit_tool: str | None = None) -> PlanResult:
        """Split remaining chapters over the days left, due reviews first."""
        with self._locked_session(workspace) as session:
            self._audit_tool(session, audit_tool, "write")
            state = session.state
            new_date = _exam_date_arg(days, exam_date)
            if new_date:
                state["exam_date"] = new_date
                policy = select_policy(st.days_left(state), "auto")
                state["mode"] = policy.name
                state["policy"] = policy.to_dict()
                reason_code, reason_params = _mode_reason("auto", st.days_left(state), policy)
                state["mode_reason"] = _mode_reason_text(
                    reason_code, reason_params, str(state.get("language") or "zh"), policy.name)
                session.apply_policy(policy)
            total = len(state["chapters"])
            done = sum(1 for c in state["chapters"] if c["status"] in ("done", "verified"))
            bank = self.load_bank(session.workspace)
            due = review_plan(session, bank)
            todo = [c for c in state["chapters"] if c["status"] == "todo"]
            ordered = order_chapters(session, todo, bank, st.days_left(state))
            groups = daily_groups(state, [c["n"] for c in ordered]) if ordered else daily_groups(state)
            counts = question_counts(bank)
            days_left = st.days_left(state)
            plan_days: list[PlanDay] = []
            for day_index, group in enumerate(groups, 1):
                entries: list[ChapterSummary] = []
                for chapter in group:
                    n, exam = counts.get(chapter["n"], (0, 0))
                    entries.append(ChapterSummary(
                        n=int(chapter["n"]), title=chapter["title"],
                        sources=list(chapter.get("sources") or []),
                        parts=int(chapter.get("parts") or 0), questions=n,
                        exam_questions=exam,
                    ))
                plan_days.append(PlanDay(day=day_index, chapters=entries))
            focus = focus_chapters(state, bank, groups)
            today_review = len(due)
            today_chapters = [c["n"] for c in groups[0]] if groups and groups[0] else []
            session.save()
            return PlanResult(
                workspace=session.workspace,
                exam_date=state.get("exam_date"),
                days_left=days_left,
                mode=state.get("mode", "longterm"),
                done=done,
                total=total,
                has_exam_date=days_left is not None,
                due=[_due_view(item) for item in due],
                days=plan_days,
                focus=[int(c["n"]) for c in focus],
                today_review_count=today_review,
                today_chapters=today_chapters,
                all_done=not groups,
                footer=self.footer_info(session),
            )

    # -------------------------------------------------------------- next
    def next(self, workspace: str | None = None, *, chars: int | None = None,
             repeat: bool = False, back: bool = False,
             audit_tool: str | None = None) -> NextResult:
        """The next teaching action (review, prerequisite, budget or material slice)."""
        with self._locked_session(workspace) as session:
            return self._next_locked(session, chars=chars, repeat=repeat, back=back,
                                     audit_tool=audit_tool)

    def _next_locked(self, session: Session, *, chars: int | None, repeat: bool,
                     back: bool, audit_tool: str | None) -> NextResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        ws = session.workspace
        materials = str(state.get("materials") or "")
        bank = self.load_bank(ws)
        recommendation = session.recommend(bank)
        footer = self.footer_info(session)

        if recommendation.kind == "due_review":
            session.save()
            return NextResult(
                workspace=ws, kind="due_review", materials=materials,
                due=[_due_view(item) for item in recommendation.due],
                params=dict(recommendation.params),
                next_command="examprep quiz --review", footer=footer,
            )
        if recommendation.kind == "prereq_blocked":
            session.save()
            return NextResult(
                workspace=ws, kind="prereq_blocked", materials=materials, chapter=recommendation.chapter,
                params=dict(recommendation.params),
                next_command="examprep goto %d" % int(recommendation.chapter or 0),
                footer=self.footer_info(session),
            )
        if recommendation.kind == "daily_limit":
            session.save()
            return NextResult(
                workspace=ws, kind="daily_limit", materials=materials, chapter=recommendation.chapter,
                params=dict(recommendation.params),
                next_command="examprep quiz --review", footer=self.footer_info(session),
            )
        if recommendation.kind == "all_done":
            session.save()
            return NextResult(workspace=ws, kind="all_done", materials=materials,
                              next_command="examprep mistakes --answers",
                              footer=self.footer_info(session))

        current = st.chapter(state)
        if current is None or current["status"] != "todo":
            target = st.chapter(state, recommendation.chapter) if recommendation.chapter is not None \
                else _advance_to_next_todo(state)
            if target is not None and target["status"] == "todo":
                state["current"] = target["n"]
                current = target
        if current is None:
            session.save()
            return NextResult(workspace=ws, kind="all_done", materials=materials,
                              next_command="examprep mistakes --answers",
                              footer=self.footer_info(session))
        chapters = self.load_chapters(ws)
        chapter = chapters.get(current["n"])
        if chapter is None:
            raise UnknownChapter(int(current["n"]), workspace=ws)
        slice_chars = chars or int(state["slice_chars"])
        parts = chapter_parts(chapter, slice_chars)
        current["parts"] = len(parts)
        if back:
            current["part"] = max(0, current["part"] - 2)
        elif repeat:
            current["part"] = max(0, current["part"] - 1)
        k = int(current["part"])
        if recommendation.kind == "new":
            session.mark_new_chapter(current["n"])
        chapter_questions = _sorted_examples(bank, current["n"])

        if not parts:
            current["part"] = 0
            session.save()
            return NextResult(
                workspace=ws, kind="no_text", materials=materials, chapter=current["n"],
                chapter_title=current["title"], parts=0,
                chapter_sources=list(chapter.sources), chapter_figures=list(chapter.figures),
                examples=chapter_questions, examples_total=len(chapter_questions),
                next_command="examprep quiz", footer=self.footer_info(session),
            )
        if k >= len(parts):
            session.save()
            return NextResult(
                workspace=ws, kind="chapter_end", materials=materials, chapter=current["n"],
                chapter_title=current["title"], chapter_sources=list(chapter.sources),
                part=len(parts), parts=len(parts),
                examples=chapter_questions, examples_total=len(chapter_questions),
                next_command="examprep quiz", footer=self.footer_info(session),
            )

        part = parts[k]
        part_text = render_part(part)
        figures = figures_for_pages(self.load_figures(ws), part_pages(part))
        chapter_figures = list(chapter.figures) if k == 0 else []
        last_part = k + 1 >= len(parts)
        current["part"] = k + 1
        session.save()
        return NextResult(
            workspace=ws, kind="material", materials=materials, chapter=current["n"],
            chapter_title=current["title"], chapter_sources=list(chapter.sources),
            part=k + 1, parts=len(parts), part_text=part_text,
            anchors=[anchor for anchor, _ in part[:6]],
            figures=figures, chapter_figures=chapter_figures,
            examples=chapter_questions if last_part else [],
            examples_total=len(chapter_questions) if last_part else 0,
            next_command="examprep quiz" if last_part else "examprep next",
            footer=self.footer_info(session),
        )

    # -------------------------------------------------------------- ask
    def ask(self, workspace: str | None = None, query: str = "", k: int = 5,
            chapter: int | None = None, chars: int = 700, *,
            provider: ModelProvider | None = None, subagent: bool = False,
            audit_tool: str | None = None) -> AskResult:
        """Search the index; return cited hits (or ``exit_code=4`` when empty)."""
        session = self.load(workspace)
        self._audit_tool(session, audit_tool, "read")
        k = max(1, min(int(k), 50))  # S3: never let a negative k become a Python slice
        hits = idx.search(idx.load_index(session.workspace), query, k=k, chapter=chapter)
        footer = self.footer_info(session)
        if not hits:
            return AskResult(workspace=session.workspace, query=query,
                             found=False, exit_code=EXIT_NO_MATERIAL, footer=footer)
        figures = self.load_figures(session.workspace)
        ask_hits = []
        for chunk, score in hits:
            text = chunk.get("text", "")
            # S1: ``chars`` is a service-level contract, not a CLI-only nicety;
            # HTTP clients get the same truncation as the terminal.
            if chars and chars > 0:
                text = shorten(text, chars)
            ask_hits.append(AskHit(
                chapter=chunk.get("chapter"), file=chunk.get("file", ""), page=chunk.get("page", 0),
                score=score, text=text,
                figures=[f.path for f in figures_for_pages(figures, [(chunk["file"], chunk["page"])])],
            ))
        summary = None
        if subagent and provider is not None:
            from .subagent import retrieval_subagent

            # B1: a sub-agent summary crosses the boundary as fenced course
            # data, never as bare model-addressed text.
            summary = gd.fence(retrieval_subagent(provider, session, query).summary)
        return AskResult(workspace=session.workspace, query=query, hits=ask_hits,
                         found=True, subagent_summary=summary, footer=footer)

    # -------------------------------------------------------------- quiz / check
    def _pick_quiz(self, session: Session, state: Mapping[str, Any],
                   bank: Sequence[Mapping[str, Any]], n: int, count: int,
                   include_all: bool, review_only: bool = False) -> list[dict[str, Any]]:
        due_cards = {card.key: card for card in session.scheduler.due()}
        if review_only:
            pool = [q for q in bank if q["id"] in due_cards]
        else:
            chapter_pool = [q for q in bank if include_all or q["chapter"] == n]
            due_pool = [q for q in bank if q["id"] in due_cards]
            due_ids = {q["id"] for q in due_pool}
            pool = due_pool + [q for q in chapter_pool if q["id"] not in due_ids]
        if not pool:
            return []
        asked = {h["qid"]: h["result"] for h in state["history"]}
        open_ids = {m["qid"] for m in st.open_mistakes(state)}

        def rank(question: Mapping[str, Any]) -> tuple[int, str, int, str]:
            exam_first = 0 if (question.get("source") or {}).get("kind") == "exam" else 1
            if question["id"] in due_cards:
                return (0, due_cards[question["id"]].due, exam_first, question["id"])
            if question["id"] in open_ids:
                return (1, "", exam_first, question["id"])
            if question["id"] not in asked:
                return (2, "", exam_first, question["id"])
            return (3, "", exam_first, question["id"])

        pool = sorted(pool, key=rank)
        return [dict(q) for q in pool[:count]]

    def quiz(self, request: QuizRequest, *, audit_tool: str | None = None) -> QuizResult:
        """Pick questions (due reviews first, open mistakes, new material questions)."""
        session = self.load(request.workspace)
        self._audit_tool(session, audit_tool, "read")
        state = session.state
        chapter = request.chapter if request.chapter is not None else state["current"]
        profile = PROFILES.get(state.get("profile") or "standard", PROFILES["standard"])
        count = request.n or min(profile["quiz_n"], session.policy.quiz_intensity)
        bank = self.load_bank(session.workspace)
        picked = self._pick_quiz(session, state, bank, int(chapter or 0), count,
                                 bool(request.all), review_only=bool(request.review))
        mode = "review" if request.review else ("all" if request.all else "chapter")
        if not picked:
            return QuizResult(workspace=session.workspace, mode=mode, chapter=chapter,
                              found=False, exit_code=EXIT_NO_QUESTIONS,
                              footer=self.footer_info(session))
        return QuizResult(workspace=session.workspace, mode=mode, chapter=chapter,
                          questions=[QuestionRecord(**q) for q in picked], found=True,
                          footer=self.footer_info(session))

    def check(self, workspace: str | None = None, qid: str = "", *,
              audit_tool: str | None = None) -> CheckResult:
        """Look up one question and its reference answer."""
        session = self.load(workspace)
        self._audit_tool(session, audit_tool, "read")
        question = self.find_question(self.load_bank(session.workspace), qid)
        if question is None:
            raise UnknownQuestion(qid, workspace=session.workspace)
        return CheckResult(workspace=session.workspace, qid=qid,
                           question=QuestionRecord(**question),
                           footer=self.footer_info(session))

    # -------------------------------------------------------------- answer / grade
    def _answer_next_command(self, session: Session) -> str:
        current = st.chapter(session.state)
        return "examprep quiz" if current and current["part"] >= current["parts"] else "examprep next"

    def grade(self, request: GradeRequest, *, provider: ModelProvider | None = None,
              audit_tool: str | None = None) -> GradeResult:
        """Grade one objective answer (sub-agent feedback optional)."""
        with self._locked_session(request.workspace) as session:
            return self._grade_locked(session, request, provider=provider, audit_tool=audit_tool)

    def _grade_locked(self, session: Session, request: GradeRequest, *,
                      provider: ModelProvider | None, audit_tool: str | None) -> GradeResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        question = self.find_question(self.load_bank(session.workspace), request.qid)
        if question is None:
            raise UnknownQuestion(request.qid, workspace=session.workspace)
        verdict, key = qmod.grade_answer(question, request.reply)
        chapter = question.get("chapter")
        feedback = None
        if request.subagent and provider is not None:
            feedback = gd.fence(grading_subagent(provider, session, question, request.reply).summary)
        if verdict == "unsure":
            return GradeResult(
                workspace=session.workspace, qid=request.qid, verdict=verdict, key=key,
                question=QuestionRecord(**question), chapter=chapter, feedback=feedback,
                next_command="examprep answer %s right|wrong" % request.qid,
                footer=self.footer_info(session),
            )
        info = session.record_answer(request.qid, int(chapter or 0), verdict)
        session.save()
        right, asked = st.chapter_results(state, int(chapter or 0))
        return GradeResult(
            workspace=session.workspace, qid=request.qid, verdict=verdict, key=key,
            recorded=True, question=QuestionRecord(**question), info=dict(info),
            chapter=chapter, chapter_right=right, chapter_asked=asked,
            open_mistakes=len(st.open_mistakes(state)), mastery=float(info["mastery"]),
            feedback=feedback, next_command=self._answer_next_command(session),
            footer=self.footer_info(session),
        )

    def answer(self, request: AnswerRequest, *, audit_tool: str | None = None) -> AnswerResult:
        """Record a right/wrong/skip result."""
        with self._locked_session(request.workspace) as session:
            return self._answer_locked(session, request, audit_tool=audit_tool)

    def _answer_locked(self, session: Session, request: AnswerRequest,
                       *, audit_tool: str | None) -> AnswerResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        question = self.find_question(self.load_bank(session.workspace), request.qid)
        if question is None:
            raise UnknownQuestion(request.qid, workspace=session.workspace)
        chapter = int(question.get("chapter") or 0)
        info = session.record_answer(request.qid, chapter, request.result, request.note or "")
        session.save()
        right, asked = st.chapter_results(state, chapter)
        return AnswerResult(
            workspace=session.workspace, qid=request.qid, result=request.result,
            chapter=chapter, chapter_right=right, chapter_asked=asked,
            open_mistakes=len(st.open_mistakes(state)), mastery=float(info["mastery"]),
            info=dict(info), next_command=self._answer_next_command(session),
            footer=self.footer_info(session),
        )

    # -------------------------------------------------------------- done / note
    def done(self, request: DoneRequest, *, audit_tool: str | None = None) -> DoneResult:
        """Mark a chapter finished, applying the mode's mastery gate."""
        with self._locked_session(request.workspace) as session:
            return self._done_locked(session, request, audit_tool=audit_tool)

    def _done_locked(self, session: Session, request: DoneRequest,
                     *, audit_tool: str | None) -> DoneResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        chapter = st.chapter(state, request.chapter) if request.chapter else st.chapter(state)
        if chapter is None:
            raise UnknownChapter(int(request.chapter or -1), workspace=session.workspace)
        right, _ = st.chapter_results(state, chapter["n"])
        mastery = session.mastery(chapter["n"])
        allowed = session.should_advance(chapter["n"])
        if not allowed and not request.force:
            return DoneResult(
                workspace=session.workspace, chapter=int(chapter["n"]), title=chapter["title"],
                status=chapter["status"], right=right, mastery=mastery, allowed=False,
                policy_label_zh=session.policy.label_zh, policy_label_en=session.policy.label_en,
                advance_mastery=float(session.policy.advance_mastery),
                footer=self.footer_info(session),
            )
        chapter["status"] = "verified" if right > 0 else "done"
        chapter["part"] = chapter["parts"]
        nxt = _advance_to_next_todo(state)
        session.save()
        return DoneResult(
            workspace=session.workspace, chapter=int(chapter["n"]), title=chapter["title"],
            status=chapter["status"], right=right, mastery=mastery, allowed=allowed,
            forced=bool(not allowed and request.force),
            policy_label_zh=session.policy.label_zh, policy_label_en=session.policy.label_en,
            advance_mastery=float(session.policy.advance_mastery),
            next_chapter=int(nxt["n"]) if nxt else None,
            next_title=nxt["title"] if nxt else None,
            all_done=nxt is None, footer=self.footer_info(session),
        )

    def note(self, request: NoteRequest, *, audit_tool: str | None = None) -> NoteResult:
        """Append a note (summary/confusion/note) to study state."""
        with self._locked_session(request.workspace) as session:
            return self._note_locked(session, request, audit_tool=audit_tool)

    def _note_locked(self, session: Session, request: NoteRequest,
                     *, audit_tool: str | None) -> NoteResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        chapter = request.chapter if request.chapter is not None else state.get("current")
        chapter = 0 if chapter is None else chapter
        state["notes"].append({
            "chapter": chapter, "type": request.type,
            "text": redact_text(request.text.strip()), "ts": st.now(),
        })
        session.save()
        current = st.chapter(state)
        is_summary = request.type == "summary"
        next_command = ("examprep done"
                        if is_summary and current and current["part"] >= current["parts"]
                        else "examprep next")
        return NoteResult(workspace=session.workspace, chapter=int(chapter), type=request.type,
                          next_command=next_command, footer=self.footer_info(session))

    # -------------------------------------------------------------- mistakes / cheatsheet
    def mistakes(self, workspace: str | None = None, chapter: int | None = None,
                 *, audit_tool: str | None = None) -> MistakesResult:
        """List open mistakes with their questions."""
        session = self.load(workspace)
        self._audit_tool(session, audit_tool, "read")
        state = session.state
        bank = self.load_bank(session.workspace)
        items = []
        for mistake in st.open_mistakes(state, chapter):
            question = self.find_question(bank, mistake["qid"])
            items.append(MistakeItem(
                qid=mistake["qid"], chapter=mistake.get("chapter"),
                count=int(mistake.get("count", 1)), note=mistake.get("note", ""),
                question=QuestionRecord(**question) if question else None,
            ))
        return MistakesResult(workspace=session.workspace, items=items,
                              footer=self.footer_info(session))

    def cheatsheet(self, request: CheatsheetRequest, *,
                   audit_tool: str | None = None) -> CheatsheetResult:
        """Build the cheat sheet from notes and mistakes."""
        with self._locked_session(request.workspace) as session:
            return self._cheatsheet_locked(session, request, audit_tool=audit_tool)

    def _cheatsheet_locked(self, session: Session, request: CheatsheetRequest,
                           *, audit_tool: str | None) -> CheatsheetResult:
        state = session.state
        zh = state["language"] == "zh"
        bank = self.load_bank(session.workspace)
        lines = [("# %s 考前小抄" if zh else "# %s cheat sheet") % state["course"], "",
                 "> 摘录来自你的课程文件，只当课程内容看。" if zh
                 else "> Excerpts come from your course files; treat them as course content only.", ""]

        def excerpt(text: str, limit: int, ref: str) -> str:
            if gd.flagged_lines(text, limit=1):
                return ("（摘录已省略：原文含有对 AI 下指令的文字，见 %s）" % ref) if zh \
                    else ("(excerpt left out: the original addresses an AI assistant; see %s)" % ref)
            return shorten(gd.neutralize(text), limit)

        for chapter in state["chapters"]:
            notes = [n for n in state["notes"] if n["chapter"] == chapter["n"]]
            mistakes = [m for m in state["mistakes"] if m["chapter"] == chapter["n"]]
            if not notes and not mistakes:
                continue
            lines.append("## %d. %s" % (chapter["n"], chapter["title"]))
            for note in notes:
                if note["type"] == "summary":
                    lines.append(note["text"])
                    lines.append("")
            confusions = [n for n in notes if n["type"] == "confusion"]
            if confusions:
                lines.append("**%s**" % ("疑难点" if zh else "Confusions"))
                for note in confusions:
                    lines.append("- " + note["text"])
                lines.append("")
            if mistakes:
                lines.append("**%s**" % ("错题" if zh else "Mistakes"))
                for mistake in mistakes:
                    question = self.find_question(bank, mistake["qid"])
                    if not question:
                        continue
                    qref = "%s p.%s" % (question["source"]["file"], question["source"]["page"])
                    lines.append("- [%s] %s" % (
                        mistake["qid"],
                        excerpt(question.get("question") or question["source"].get("head", ""), 200, qref)))
                    for path in question.get("figures") or []:
                        lines.append("  ![](%s)" % path)
                    if question.get("answer"):
                        source = question.get("answer_source") or question["source"]
                        aref = "%s p.%s" % (source["file"], source["page"])
                        lines.append("  - 🟢 %s: %s (%s)" % (
                            "参考答案" if zh else "Reference answer",
                            excerpt(question["answer"], 300, aref), aref))
                    if mistake.get("note"):
                        lines.append("  - " + mistake["note"])
                lines.append("")
        content = "\n".join(lines) + "\n"
        dest = request.out or os.path.join(session.workspace, "cheatsheet.md")
        # The gate call is the audit record for this write path; transports
        # pass their own tool name so CLI/HTTP/MCP are distinguishable.
        decision = session.gate.check_path(dest, "write", tool=audit_tool or "cheatsheet")
        if not decision.allowed:
            return CheatsheetResult(workspace=session.workspace, path=dest, content=content,
                                    written=False, denied_reason=decision.reason,
                                    exit_code=EXIT_USAGE, footer=self.footer_info(session))
        atomic_write_text(dest, content,
                          snapshot_root=os.path.join(session.workspace, ".snapshots"))
        return CheatsheetResult(workspace=session.workspace, path=dest, content=content,
                                written=True, footer=self.footer_info(session))

    # -------------------------------------------------------------- goto / graph
    def goto(self, request: GotoRequest, *, audit_tool: str | None = None) -> GotoResult:
        """Switch chapters, refusing while a prerequisite is unmet (unless forced)."""
        with self._locked_session(request.workspace) as session:
            return self._goto_locked(session, request, audit_tool=audit_tool)

    def _goto_locked(self, session: Session, request: GotoRequest,
                     *, audit_tool: str | None) -> GotoResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        chapter = st.chapter(state, request.chapter)
        if chapter is None:
            raise UnknownChapter(request.chapter, workspace=session.workspace)
        gaps = session.unmet_prerequisites(request.chapter)
        footer = self.footer_info(session)
        if gaps and not request.force:
            gap = gaps[0]
            return GotoResult(
                workspace=session.workspace, chapter=request.chapter, title=chapter["title"],
                switched=False, blocked=True,
                gap=PrereqGapView(kp=gap.kp, chapter=gap.chapter, title=gap.title,
                                  mastery=gap.mastery, threshold=gap.threshold),
                next_command="examprep goto %d" % gap.chapter, footer=footer,
            )
        state["current"] = request.chapter
        if chapter["status"] != "todo":
            chapter["status"] = "todo"
        if request.restart:
            chapter["part"] = 0
        session.save()
        gap_view = None
        if gaps:
            gap = gaps[0]
            gap_view = PrereqGapView(kp=gap.kp, chapter=gap.chapter, title=gap.title,
                                     mastery=gap.mastery, threshold=gap.threshold)
        return GotoResult(
            workspace=session.workspace, chapter=request.chapter, title=chapter["title"],
            switched=True, blocked=False, forced=bool(gaps), gap=gap_view,
            next_command="examprep next", footer=self.footer_info(session),
        )

    def graph(self, request: GraphRequest, *, audit_tool: str | None = None) -> GraphResult:
        """Show the prerequisite graph and apply user revisions."""
        with self._locked_session(request.workspace) as session:
            return self._graph_locked(session, request, audit_tool=audit_tool)

    def _graph_locked(self, session: Session, request: GraphRequest,
                      *, audit_tool: str | None) -> GraphResult:
        self._audit_tool(session, audit_tool, "write")
        state = session.state
        bank = self.load_bank(session.workspace)
        edits = state.setdefault("graph_edits", {"add": [], "remove": []})
        edits.setdefault("add", [])
        edits.setdefault("remove", [])
        # I6: apply edits transactionally.  A rejected edge must not leave the
        # successful half persisted; roll back in-memory state and the saved
        # edit list, then return every error.
        original_edges = list(session.graph.edges)
        original_edits = copy.deepcopy(edits)
        changed = False
        messages: list[dict[str, Any]] = []
        errors: list[str] = []
        for text in request.add_prereq:
            pair = _parse_edge(text)
            if pair is None:
                errors.append("edge-hint:%s" % text)
                continue
            prerequisite, target = pair
            if not session.graph.add_edge(prerequisite, target, reason="user"):
                errors.append("invalid:%s" % text)
                continue
            if [prerequisite, target] not in edits["add"]:
                edits["add"].append([prerequisite, target])
            edits["remove"] = [item for item in edits["remove"] if item != [prerequisite, target]]
            messages.append({"action": "add", "prerequisite": prerequisite, "target": target})
            changed = True
        for text in request.remove_prereq:
            pair = _parse_edge(text)
            if pair is None:
                errors.append("edge-hint:%s" % text)
                continue
            prerequisite, target = pair
            if not session.graph.remove_edge(prerequisite, target):
                errors.append("invalid:%s" % text)
                continue
            if [prerequisite, target] not in edits["remove"]:
                edits["remove"].append([prerequisite, target])
            edits["add"] = [item for item in edits["add"] if item != [prerequisite, target]]
            messages.append({"action": "remove", "prerequisite": prerequisite, "target": target})
            changed = True
        if request.rebuild and not errors:
            chapters = self.load_chapters(session.workspace)
            rebuilt = build_graph(chapters.values())
            rebuilt.apply_edits(edits)
            session.graph = rebuilt
            changed = True
        if errors:
            session.graph.edges = original_edges
            state["graph_edits"] = original_edits
            changed = False
            messages = []
        if changed:
            session.save()
            session.save_graph()
        stats = session.graph_stats(bank)
        threshold = float(session.policy.advance_mastery)
        nodes: list[GraphNodeView] = []
        for kp in session.graph.topological_order():
            node = session.graph.nodes[kp]
            prerequisites = [
                {"from": edge.prerequisite, "reason": edge.reason}
                for edge in session.graph.edges if edge.target == kp
            ]
            nodes.append(GraphNodeView(
                kp=kp, title=node.title, mastery=session.learner.mastery(kp),
                unlocked=session.graph.is_unlocked(kp, session.learner.mastery, threshold),
                prerequisites=prerequisites,
            ))
        return GraphResult(
            workspace=session.workspace, nodes=nodes,
            edge_count=len(session.graph.edges), stats=stats, edits=messages,
            errors=errors, changed=changed, exit_code=EXIT_USAGE if errors else 0,
            footer=self.footer_info(session),
        )

    # -------------------------------------------------------------- permissions
    def permissions(self, request: PermissionRequest, *,
                    audit_tool: str | None = None) -> PermissionsResult:
        """List, grant or revoke persistent L2 authorizations."""
        with self._locked_session(request.workspace) as session:
            return self._permissions_locked(session, request, audit_tool=audit_tool)

    def _permissions_locked(self, session: Session, request: PermissionRequest,
                            *, audit_tool: str | None) -> PermissionsResult:
        self._audit_tool(session, audit_tool, "read" if request.action == "list" else "write")
        if request.action == "list":
            grants = [
                PermissionView(id=g.id, kind=g.kind, access=list(g.access), value=g.value, note=g.note)
                for g in session.permissions.list()
            ]
            return PermissionsResult(workspace=session.workspace, action="list", grants=grants,
                                     message_code="permissions_head")
        if request.action == "grant":
            if request.capability:
                grant = session.gate.grant_capability(request.capability, note=request.note or "")
                return PermissionsResult(
                    workspace=session.workspace, action="grant", message_code="permission_capability",
                    message_params={"id": grant.id, "value": grant.value},
                    grant=PermissionView(id=grant.id, kind=grant.kind, access=list(grant.access),
                                         value=grant.value, note=grant.note),
                )
            if not request.target:
                return PermissionsResult(workspace=session.workspace, action="grant",
                                         message_code="permissions_usage", exit_code=EXIT_USAGE)
            target = session.path_policy.resolve(request.target)
            if session.path_policy.is_material(target):
                return PermissionsResult(
                    workspace=session.workspace, action="grant",
                    message_code="permission_materials",
                    message_params={"target": target}, exit_code=EXIT_USAGE,
                )
            try:
                validate_grant_target(target)
            except ValueError as exc:
                return PermissionsResult(
                    workspace=session.workspace, action="grant",
                    message_code="grant_refused", message_params={"target": target, "error": str(exc)},
                    exit_code=EXIT_USAGE,
                )
            access = ("read",) if request.read_only else ("read", "write")
            try:
                grant = session.gate.grant_directory(target, access=access, note=request.note or "")
            except ValueError as exc:
                return PermissionsResult(
                    workspace=session.workspace, action="grant",
                    message_code="grant_refused", message_params={"target": target, "error": str(exc)},
                    exit_code=EXIT_USAGE,
                )
            return PermissionsResult(
                workspace=session.workspace, action="grant", message_code="permission_granted",
                message_params={"id": grant.id, "value": grant.value,
                                "access": ",".join(grant.access)},
                grant=PermissionView(id=grant.id, kind=grant.kind, access=list(grant.access),
                                     value=grant.value, note=grant.note),
            )
        # revoke
        if not request.target:
            return PermissionsResult(workspace=session.workspace, action="revoke",
                                     message_code="permission_not_found", exit_code=EXIT_USAGE)
        removed = session.gate.revoke(request.target)
        return PermissionsResult(
            workspace=session.workspace, action="revoke", removed=removed,
            message_code="permission_revoked" if removed else "permission_not_found",
            message_params={"target": request.target},
            exit_code=0 if removed else EXIT_USAGE,
        )

    # -------------------------------------------------------------- costs
    def costs(self, workspace: str | None = None, *, input_price: float | None = None,
              output_price: float | None = None, command: str | None = None) -> CostsResult:
        """Aggregate token usage/cost from the workspace's JSONL event logs.

        Unlike study commands this only needs a directory with ``logs/`` — a
        bare workspace copy (or an older workspace) can still be priced.
        """
        if workspace:
            path = os.path.abspath(workspace)
            if not os.path.isdir(path):
                raise WorkspaceNotFound("no such workspace directory: %s" % path)
        else:
            path = self.require_workspace(None)
        try:
            return aggregate_costs(path, input_price=input_price, output_price=output_price,
                                   command=command)
        except ValueError as exc:  # non-finite unit price (NaN/inf) is a usage error
            raise ServiceError(str(exc), code="bad_price", exit_code=EXIT_USAGE) from exc


# ------------------------------------------------------------------ helpers
def _read_json(path: str, default: Any = None) -> Any:
    import json

    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: str, payload: Any, snapshot_root: str | None = None) -> None:
    from ..security.atomicio import atomic_write_json

    atomic_write_json(path, payload, snapshot_root=snapshot_root)


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _exam_date(request: SetupRequest) -> str | None:
    """Exam date from ``--date``/``--days`` (setup request)."""
    return _exam_date_arg(request.days, request.date)


def _exam_date_arg(days: int | None, exam_date: str | None) -> str | None:
    import datetime as _dt

    if exam_date:
        try:
            return _dt.date.fromisoformat(str(exam_date)).isoformat()
        except ValueError as exc:
            raise ServiceError("--date must look like 2026-12-15", code="bad_date",
                               exit_code=EXIT_USAGE) from exc
    if days is not None:
        return st.exam_date_in(days)
    return None


def _mode_reason(requested: str, days: int | None, policy: StudyPolicy) -> tuple[str, dict[str, Any]]:
    if requested not in ("auto", "", None):
        return "manual", {"mode": policy.name}
    if days is None:
        return "auto_none", {}
    if policy.name == "cram":
        return "auto_cram", {"days": days, "threshold": LONGTERM_THRESHOLD_DAYS}
    return "auto_long", {"days": days, "threshold": LONGTERM_THRESHOLD_DAYS}


def _mode_reason_text(code: str, params: Mapping[str, Any], lang: str, mode: str) -> str:
    """P1-compatible human-readable ``study_state.json`` field.

    The value is persisted state (external consumers may read it), so it keeps
    the exact P1 wording and language rather than the structured code/params
    that only the transports consume.
    """
    if code == "manual":
        return ("手动指定 %s 模式" if lang == "zh" else "manually selected %s mode") % mode
    if code == "auto_none":
        return ("未设置考试日期，默认长期模式" if lang == "zh"
                else "no exam date set; defaulting to long-term mode")
    if code == "auto_cram":
        return ("距考试 %d 天 < %d，突击模式" if lang == "zh"
                else "%d day(s) to exam < %d; cram mode") % (
                    params.get("days"), params.get("threshold"))
    return ("距考试 %d 天 ≥ %d，长期模式" if lang == "zh"
            else "%d day(s) to exam ≥ %d; long-term mode") % (
                params.get("days"), params.get("threshold"))


def _due_view(item: Any) -> DueReviewView:
    return DueReviewView(
        qid=item.qid, kp=item.kp, chapter=item.chapter, due=item.due,
        retrievability=item.retrievability, question=item.question,
        source=dict(item.source or {}),
    )


def _sorted_examples(bank: Sequence[Mapping[str, Any]], chapter: int) -> list[QuestionRecord]:
    items = [dict(q) for q in bank if q.get("chapter") == chapter]
    items.sort(key=lambda q: (q.get("source") or {}).get("kind") != "exam")  # past exams first
    return [QuestionRecord(**item) for item in items]


def _parse_edge(text: str) -> tuple[str, str] | None:
    """Parse ``PREREQ:TARGET`` (also accepts ``->`` / ``→``)."""
    for separator in ("->", "→", ":"):
        if separator in text:
            left, _, right = text.partition(separator)
            left, right = left.strip(), right.strip()
            return (left, right) if left and right else None
    return None


__all__ = [
    "StudyService", "chapter_parts", "render_part", "part_pages",
    "figures_for_pages", "daily_groups", "days_for_study", "question_counts",
    "focus_chapters", "attach_question_figures", "PROFILES", "DEFAULT_POINTER",
]
