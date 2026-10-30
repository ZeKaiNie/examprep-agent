# -*- coding: utf-8 -*-
"""Deterministic teaching-loop planner.

Priority order for ``next`` and ``plan``:

1. **Due FSRS reviews** — review debt always comes first; the card's mastery and
   question mapping decide what is shown.
2. **Unmet prerequisites** — if the target KP is blocked, the oldest/deepest gap
   is recommended instead, with a concrete reason.
3. **Daily new-learning budget** — once the policy's ``daily_new_limit`` is
   reached, the planner sends the learner to review instead of starting more
   new chapters.
4. **New material** — chapter order in long-term mode; in cram mode chapters are
   ranked by ``urgency × past-exam frequency × (1 − mastery)``.

The planner returns structured recommendations; the CLI owns the bilingual
wording.  It is pure with respect to the session (apart from the explicit daily
counter helpers) and therefore easy to test.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .graph import KnowledgeGraph, kp_for_chapter


@dataclass(frozen=True)
class DueReview:
    """One card that is due today, enriched with its question/KP."""

    qid: str
    kp: str | None
    chapter: int | None
    due: str
    retrievability: float
    question: str = ""
    source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view."""
        return {
            "qid": self.qid,
            "kp": self.kp,
            "chapter": self.chapter,
            "due": self.due,
            "retrievability": self.retrievability,
            "question": self.question,
        }


@dataclass(frozen=True)
class PrereqGap:
    """An unmet prerequisite for a target KP."""

    kp: str
    chapter: int
    title: str
    mastery: float
    threshold: float


@dataclass(frozen=True)
class Recommendation:
    """One planner decision plus the data needed to explain it."""

    kind: str                    # due_review | prereq_blocked | daily_limit | new | continue | all_done
    reason_code: str
    kp: str | None = None
    chapter: int | None = None
    qid: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    due: tuple[DueReview, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (used by tests and future MCP interfaces)."""
        return {
            "kind": self.kind,
            "reason_code": self.reason_code,
            "kp": self.kp,
            "chapter": self.chapter,
            "qid": self.qid,
            "params": dict(self.params),
            "due": [item.to_dict() for item in self.due],
        }


# ---------------------------------------------------------------- daily counters
def daily_counters(state: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Return today's counters, resetting them when the calendar day changes."""
    day = (today or date.today()).isoformat()
    data = state.setdefault("daily", {})
    if not isinstance(data, dict) or data.get("date") != day:
        data = {"date": day, "new_chapters": [], "reviews": 0, "answers": 0}
        state["daily"] = data
    data.setdefault("new_chapters", [])
    data.setdefault("reviews", 0)
    data.setdefault("answers", 0)
    return data


def mark_new_chapter(state: dict[str, Any], chapter_n: int, today: date | None = None) -> bool:
    """Record that a new chapter was started today; returns False if already counted."""
    counters = daily_counters(state, today)
    if chapter_n in counters["new_chapters"]:
        return False
    counters["new_chapters"].append(chapter_n)
    return True


def mark_review(state: dict[str, Any], today: date | None = None) -> None:
    """Increment today's review counter."""
    daily_counters(state, today)["reviews"] += 1


# ---------------------------------------------------------------- helpers
def question_map(bank: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Question id → question record."""
    return {str(question.get("id")): question for question in bank if question.get("id") is not None}


def _chapter_of(question: Mapping[str, Any] | None) -> int | None:
    if question is None:
        return None
    try:
        return int(question.get("chapter"))
    except (TypeError, ValueError):
        return None


def due_reviews(session: Any, bank: Sequence[Mapping[str, Any]], today: date | None = None) -> list[DueReview]:
    """FSRS cards due today, mapped to questions and knowledge points."""
    questions = question_map(bank)
    result: list[DueReview] = []
    for card in session.scheduler.due(today):
        question = questions.get(card.key)
        chapter = _chapter_of(question)
        kp = getattr(card, "kp", None) or (kp_for_chapter(chapter) if chapter is not None else None)
        try:
            retain = session.scheduler.retrievability(card, today)
        except (AttributeError, TypeError, ValueError):
            retain = 0.0
        question_text = ""
        source: dict[str, Any] = {}
        if question is not None:
            question_text = str(question.get("question") or question.get("source", {}).get("head", ""))
            source = dict(question.get("source") or {})
        result.append(DueReview(
            qid=card.key,
            kp=kp,
            chapter=chapter,
            due=card.due,
            retrievability=retain,
            question=question_text,
            source=source,
        ))
    return result


def due_kps(session: Any, bank: Sequence[Mapping[str, Any]], today: date | None = None) -> set[str]:
    """Knowledge points that have at least one due card."""
    return {item.kp for item in due_reviews(session, bank, today) if item.kp}


def unmet_prerequisites(session: Any, target: int | str) -> list[PrereqGap]:
    """Unmet prerequisite chain for a chapter number or ``chN`` id."""
    kp = kp_for_chapter(target) if isinstance(target, int) else str(target)
    graph: KnowledgeGraph = session.graph
    if kp not in graph.nodes:
        # A missing target must not silently count as "unlocked": rebuild the
        # graph from study state (the bounded fallback) and try once more.
        from .graph import graph_from_state

        rebuilt = graph_from_state(session.state)
        rebuilt.apply_edits(session.state.get("graph_edits"))
        if kp in rebuilt.nodes:
            session.graph = rebuilt
            graph = rebuilt
    mastery = session.learner.mastery
    threshold = float(session.policy.advance_mastery)
    gaps: list[PrereqGap] = []
    for prereq, value in graph.blocking_chain(kp, mastery, threshold):
        node = graph.nodes.get(prereq)
        if node is None:
            continue
        gaps.append(PrereqGap(
            kp=prereq,
            chapter=node.chapter,
            title=node.title,
            mastery=float(value),
            threshold=threshold,
        ))
    return gaps


def cram_score(session: Any, chapter: Mapping[str, Any], bank: Sequence[Mapping[str, Any]],
               days_left: int | None) -> float:
    """Past-exam frequency × unmastered share × chapter-specific time pressure.

    The exam date is *not* a single global multiplier (which left the chapter
    order unchanged): each chapter's practice debt (questions × unmastered) is
    spread over the quiz intensity to estimate the days it needs.  When the
    remaining days fall below that estimate, the chapter is boosted — so
    shrinking the runway can genuinely reorder chapters (a high-debt chapter
    overtakes a high-frequency but small one).
    """
    number = int(chapter.get("n", 0))
    total = exam = 0
    for question in bank:
        if _chapter_of(question) != number:
            continue
        total += 1
        if (question.get("source") or {}).get("kind") == "exam":
            exam += 1
    mastery = float(session.learner.mastery(kp_for_chapter(number)))
    frequency = 1.0 + 2.0 * exam + 0.2 * total
    unmastered = max(0.05, 1.0 - mastery)
    per_day = max(1, int(getattr(session.policy, "quiz_intensity", 3)))
    needed_days = max(1.0, math.ceil(max(1, total) * unmastered / per_day))
    if days_left is None:
        urgency = 1.0
    else:
        days = max(1, int(days_left))
        urgency = 1.0 + 2.0 * max(0.0, needed_days - days) / needed_days
    return frequency * unmastered * urgency


def order_chapters(
    session: Any,
    chapters: Sequence[Mapping[str, Any]],
    bank: Sequence[Mapping[str, Any]],
    days_left: int | None = None,
) -> list[Mapping[str, Any]]:
    """Order todo chapters for the active mode (cram ranking or graph order)."""
    if getattr(session, "mode", "longterm") == "cram":
        return sorted(
            chapters,
            key=lambda chapter: (-cram_score(session, chapter, bank, days_left), int(chapter.get("n", 0))),
        )
    order = {kp: index for index, kp in enumerate(session.graph.topological_order())}
    return sorted(
        chapters,
        key=lambda chapter: (order.get(kp_for_chapter(int(chapter.get("n", 0))), 10_000),
                             int(chapter.get("n", 0))),
    )


def target_chapter(
    session: Any,
    bank: Sequence[Mapping[str, Any]],
    today: date | None = None,
) -> Mapping[str, Any] | None:
    """Current chapter when it is still todo, else the mode's next todo chapter."""
    state = session.state
    current = None
    for chapter in state.get("chapters", []):
        if chapter.get("n") == state.get("current"):
            current = chapter
            break
    if current is not None and current.get("status") == "todo":
        return current
    todo = [chapter for chapter in state.get("chapters", []) if chapter.get("status") == "todo"]
    if not todo:
        return None
    days = state.get("exam_date")
    days_left = None
    if days:
        try:
            days_left = max(0, (date.fromisoformat(str(days)) - (today or date.today())).days)
        except ValueError:
            days_left = None
    ordered = order_chapters(session, todo, bank, days_left)
    return ordered[0] if ordered else None


# ---------------------------------------------------------------- recommendation
def _due_recommendation(reviews: Sequence[DueReview]) -> Recommendation:
    return Recommendation(
        kind="due_review",
        reason_code="due_review",
        kp=reviews[0].kp,
        chapter=reviews[0].chapter,
        qid=reviews[0].qid,
        params={"count": len(reviews)},
        due=tuple(reviews),
    )


def recommend(session: Any, bank: Sequence[Mapping[str, Any]], today: date | None = None) -> Recommendation:
    """The single next action, in priority order (see module docstring).

    ``policy.review_first`` is honoured: long-term mode puts review debt ahead
    of new material, while cram mode (``review_first=False``) spends the runway
    on new/high-yield material first and still recommends reviews when no new
    chapter is left.  Due cards remain visible through ``plan``/``quiz --review``
    in both modes.
    """
    reviews = due_reviews(session, bank, today)
    review_first = bool(getattr(session.policy, "review_first", True))
    if reviews and review_first:
        return _due_recommendation(reviews)

    target = target_chapter(session, bank, today)
    if target is None:
        return _due_recommendation(reviews) if reviews else Recommendation(
            kind="all_done", reason_code="all_done")

    gaps = unmet_prerequisites(session, int(target.get("n", 0)))
    if gaps:
        gap = gaps[0]
        return Recommendation(
            kind="prereq_blocked",
            reason_code="prereq_blocked",
            kp=gap.kp,
            chapter=gap.chapter,
            params={
                "target": int(target.get("n", 0)),
                "target_title": str(target.get("title", "")),
                "prereq_title": gap.title,
                "mastery": gap.mastery,
                "threshold": gap.threshold,
                "chain": [item.kp for item in gaps],
            },
        )

    counters = daily_counters(session.state, today)
    number = int(target.get("n", 0))
    starting_new = int(target.get("part", 0)) == 0 and number not in counters["new_chapters"]
    if starting_new and len(counters["new_chapters"]) >= int(session.policy.daily_new_limit):
        return Recommendation(
            kind="daily_limit",
            reason_code="daily_new_limit",
            chapter=number,
            params={
                "limit": int(session.policy.daily_new_limit),
                "used": len(counters["new_chapters"]),
                "target": number,
            },
        )

    params: dict[str, Any] = {"part": int(target.get("part", 0))}
    if reviews:
        # review_first=False spends the runway on new material first, but the
        # outstanding review debt stays visible to callers.
        params["due_pending"] = len(reviews)
    return Recommendation(
        kind="new" if starting_new else "continue",
        reason_code="new" if starting_new else "continue",
        kp=kp_for_chapter(number),
        chapter=number,
        params=params,
    )


def review_plan(session: Any, bank: Sequence[Mapping[str, Any]], today: date | None = None) -> list[DueReview]:
    """Due review queue for ``plan`` (empty when nothing is due)."""
    return due_reviews(session, bank, today)


def count_new_today(state: Mapping[str, Any], today: date | None = None) -> int:
    """Number of new chapters started today (read-only view)."""
    day = (today or date.today()).isoformat()
    data = state.get("daily") or {}
    if data.get("date") != day:
        return 0
    return len(data.get("new_chapters") or [])
