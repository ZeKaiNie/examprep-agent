# -*- coding: utf-8 -*-
"""study_state.json: the one file that remembers progress. progress.md and notebook.md are views.

Derived from universal-examprep-skill (MIT, (c) ZeKaiNie):
https://github.com/ZeKaiNie/universal-examprep-skill
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

from ..security.atomicio import atomic_write_text, snapshot_candidates
from ..security.audit import redact_text

STATE_FILE = "study_state.json"
PROGRESS_FILE = "progress.md"
NOTEBOOK_FILE = "notebook.md"


def now() -> str:
    """Current local time in the workspace's human-readable format.

    Milliseconds keep two answers in the same minute distinguishable, which the
    memory import deduplication relies on.
    """
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def exam_date_in(days: int | None, today: _dt.date | None = None) -> str | None:
    """ISO date `days` from today (the exam day), or None."""
    if days is None:
        return None
    return ((today or _dt.date.today()) + _dt.timedelta(days=max(0, days))).isoformat()


def days_left(state: dict[str, Any], today: _dt.date | None = None) -> int | None:
    """Whole days until the exam date (0 on the day itself), or None when no date is set."""
    d = state.get("exam_date")
    if not d:
        return None
    try:
        exam = _dt.date.fromisoformat(d)
    except ValueError:
        return None
    return max(0, (exam - (today or _dt.date.today())).days)


def new_state(
    course: str,
    language: str,
    exam_date: str | None,
    materials: str,
    chapters: list[dict[str, Any]],
    slice_chars: int,
    profile: str = "standard",
) -> dict[str, Any]:
    """Build a fresh v5 study state for a newly set-up workspace."""
    return {
        "version": 5,
        "course": course,
        "language": language,
        "exam_date": exam_date,
        "profile": profile,
        "materials": materials,
        "created": now(),
        "updated": now(),
        "slice_chars": slice_chars,
        "current": chapters[0]["n"] if chapters else None,
        "chapters": [
            {"n": c["n"], "title": c["title"], "status": "todo", "part": 0, "parts": c["parts"],
             "sources": c["sources"], "questions": c["questions"]}
            for c in chapters
        ],
        "history": [],   # {"qid","result","ts","chapter"}
        "mistakes": [],  # {"qid","chapter","note","status": open|fixed,"count"}
        "notes": [],     # {"chapter","type": summary|confusion|note,"text","ts"}
        # P1 learner-model fields (safe defaults for older workspaces):
        "graph_edits": {"add": [], "remove": []},
        "daily": {},
        "security_scan": {"scanned": False, "findings": []},
    }


def path(ws: str) -> str:
    """Absolute path of the study state file inside a workspace."""
    return os.path.join(ws, STATE_FILE)


def load(ws: str) -> dict[str, Any] | None:
    """Load a workspace state, migrating older fields and recovering snapshots.

    A truncated/corrupt state file is not fatal: the newest atomic snapshot is
    tried before raising, so an interrupted write cannot destroy a course.
    """
    p = path(ws)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            state = json.load(fh)
    except (ValueError, TypeError):
        state = None
        for snapshot in snapshot_candidates(p, os.path.join(ws, ".snapshots")):
            try:
                with open(snapshot, encoding="utf-8") as fh:
                    state = json.load(fh)
                break
            except (ValueError, TypeError, OSError):
                continue
        if state is None:
            raise
    if not isinstance(state, dict):
        raise ValueError("study_state.json is not an object")
    if "exam_days" in state:  # before 5.3 the count was stored and never went down
        days = state.pop("exam_days")
        if days is not None and not state.get("exam_date"):
            try:
                start = _dt.date.fromisoformat(state.get("created", "")[:10])
            except ValueError:
                start = _dt.date.today()
            state["exam_date"] = exam_date_in(days, start)
    state.setdefault("exam_date", None)
    state.setdefault("profile", "standard")
    state.setdefault("graph_edits", {"add": [], "remove": []})
    state.setdefault("daily", {})
    state.setdefault("security_scan", {"scanned": False, "findings": []})
    return state


def _atomic_write(p: str, text: str, snapshot_root: str | None = None) -> None:
    """Atomic write with unique temp names and a pre-write snapshot.

    The unique temp file (``mkstemp`` in the destination directory) means two
    processes writing the same workspace no longer collide on a fixed ``.tmp``
    name; ``os.replace`` keeps readers on a complete document.
    """
    atomic_write_text(p, text, snapshot_root=snapshot_root)


def save(ws: str, state: dict[str, Any]) -> None:
    """Persist state plus the human-readable progress.md and notebook.md views."""
    state["updated"] = now()
    snapshot_root = os.path.join(ws, ".snapshots")
    _atomic_write(path(ws), json.dumps(state, ensure_ascii=False, indent=1), snapshot_root)
    _atomic_write(os.path.join(ws, PROGRESS_FILE), render_progress(state), snapshot_root)
    _atomic_write(os.path.join(ws, NOTEBOOK_FILE), render_notebook(state), snapshot_root)


def chapter(state: dict[str, Any], n: int | None = None) -> dict[str, Any] | None:
    """The chapter dict for number `n` (default: the current chapter), or None."""
    n = state["current"] if n is None else n
    for c in state["chapters"]:
        if c["n"] == n:
            return c
    return None


def open_mistakes(state: dict[str, Any], n: int | None = None) -> list[dict[str, Any]]:
    """Open mistakes, optionally limited to one chapter."""
    return [m for m in state["mistakes"] if m["status"] == "open" and (n is None or m["chapter"] == n)]


def record_result(
    state: dict[str, Any],
    qid: str,
    chapter_n: int,
    result: str,
    note: str = "",
) -> None:
    """Append a history entry and open/close the matching mistake."""
    note = redact_text(str(note or ""))
    state["history"].append({"qid": qid, "result": result, "ts": now(), "chapter": chapter_n})
    existing = next((m for m in state["mistakes"] if m["qid"] == qid), None)
    if result in ("wrong", "skip"):
        if existing:
            existing["count"] += 1
            existing["status"] = "open"
            if note:
                existing["note"] = note
        else:
            state["mistakes"].append({"qid": qid, "chapter": chapter_n, "note": note, "status": "open", "count": 1})
    elif result == "right" and existing and existing["status"] == "open":
        existing["status"] = "fixed"


def chapter_results(state: dict[str, Any], n: int) -> tuple[int, int]:
    """(right answers, questions asked) recorded for a chapter."""
    right = sum(1 for h in state["history"] if h["chapter"] == n and h["result"] == "right")
    asked = sum(1 for h in state["history"] if h["chapter"] == n)
    return right, asked


def bar(done: int, total: int, width: int = 10) -> str:
    """A simple textual progress bar."""
    filled = int(round(width * done / total)) if total else 0
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def render_progress(state: dict[str, Any]) -> str:
    """Render progress.md from state."""
    zh = state["language"] == "zh"
    done = sum(1 for c in state["chapters"] if c["status"] in ("done", "verified"))
    total = len(state["chapters"])
    lines = ["# %s" % ("复习进度" if zh else "Study progress"), ""]
    lines.append("- %s: %s" % ("课程" if zh else "Course", state["course"]))
    lines.append("- %s: %s" % ("更新" if zh else "Updated", state["updated"]))
    lines.append("- %s: %s %d/%d" % ("章节" if zh else "Chapters", bar(done, total), done, total))
    lines.append("")
    lines.append("| # | %s | %s | %s |" % (("标题", "状态", "测验") if zh else ("Title", "Status", "Quiz")))
    lines.append("| --- | --- | --- | --- |")
    for c in state["chapters"]:
        right, asked = chapter_results(state, c["n"])
        mark = "→ " if c["n"] == state["current"] else ""
        lines.append("| %s%d | %s | %s | %d/%d |" % (mark, c["n"], c["title"], c["status"], right, asked))
    lines.append("")
    lines.append("## %s" % ("错题" if zh else "Mistakes"))
    om = [m for m in state["mistakes"] if m["status"] == "open"]
    if not om:
        lines.append("- (%s)" % ("暂无" if zh else "none"))
    for m in om:
        lines.append("- %s (ch%d, ×%d) %s" % (m["qid"], m["chapter"], m["count"], m.get("note", "")))
    lines.append("")
    conf = [n for n in state["notes"] if n["type"] == "confusion"]
    lines.append("## %s" % ("疑难点" if zh else "Confusions"))
    if not conf:
        lines.append("- (%s)" % ("暂无" if zh else "none"))
    for n in conf:
        lines.append("- ch%s: %s" % (n["chapter"], n["text"]))
    return "\n".join(lines) + "\n"


def render_notebook(state: dict[str, Any]) -> str:
    """Render notebook.md from state."""
    zh = state["language"] == "zh"
    lines = ["# %s" % ("学习笔记" if zh else "Notebook"), ""]
    for c in state["chapters"]:
        notes = [n for n in state["notes"] if n["chapter"] == c["n"]]
        if not notes:
            continue
        lines.append("## %s %d: %s" % ("第" if zh else "Chapter", c["n"], c["title"]) if not zh
                     else "## 第 %d 章：%s" % (c["n"], c["title"]))
        for n in notes:
            lines.append("- [%s %s] %s" % (n["type"], n["ts"], n["text"]))
        lines.append("")
    return "\n".join(lines) + "\n"
