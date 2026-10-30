# -*- coding: utf-8 -*-
"""Dual-mode study policies: long-term learning vs. short-term cram.

Both modes share the same engine; only the scheduling knobs differ:

- :class:`LongTermPolicy` — FSRS-led retention, BKT mastery gates, controlled new
  material and review debt before new content.
- :class:`CramPolicy` — exam-frequency triage, high quiz intensity, lower mastery
  gate, less concern with long-term retention.

:func:`select_policy` picks a policy from the days left until the exam
(``>= 21`` days → long-term, ``< 21`` → cram) unless the caller overrides it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

LONGTERM_THRESHOLD_DAYS = 21


@dataclass(frozen=True)
class StudyPolicy:
    """Shared policy surface; subclasses override the defaults."""

    name: str = "standard"
    label_zh: str = "标准"
    label_en: str = "Standard"
    new_vs_review: float = 0.5      # share of daily effort spent learning new material
    quiz_intensity: int = 3         # questions per quiz session
    advance_mastery: float = 0.75   # BKT p(known) needed before advancing
    target_retention: float = 0.9   # FSRS desired retention
    daily_new_limit: int = 6        # new chapters/questions per day
    review_first: bool = True       # review debt before new material

    @property
    def new_share(self) -> float:
        """New-material share of daily effort."""
        return self.new_vs_review

    @property
    def review_share(self) -> float:
        """Review share of daily effort."""
        return 1.0 - self.new_vs_review

    def can_advance(self, mastery: float, accuracy: float = 1.0) -> bool:
        """Advancement gate: BKT mastery at/above the policy threshold.

        Accuracy is accepted for future policies that combine both signals; the
        P0 gate is mastery-only.  Long-term's 0.8 needs more than one correct
        answer; cram's lower 0.55 can be cleared by one correct answer by design.
        """
        return mastery >= self.advance_mastery

    def to_dict(self) -> dict[str, object]:
        """Plain-dict view suitable for study_state.json."""
        return asdict(self)


@dataclass(frozen=True)
class LongTermPolicy(StudyPolicy):
    """Retention-first policy for months-long preparation."""

    name: str = "longterm"
    label_zh: str = "长期模式"
    label_en: str = "Long-term"
    new_vs_review: float = 0.4
    quiz_intensity: int = 3
    advance_mastery: float = 0.8
    target_retention: float = 0.9
    daily_new_limit: int = 4
    review_first: bool = True


@dataclass(frozen=True)
class CramPolicy(StudyPolicy):
    """Score-per-minute policy for exams days away."""

    name: str = "cram"
    label_zh: str = "突击模式"
    label_en: str = "Cram"
    new_vs_review: float = 0.75
    quiz_intensity: int = 5
    advance_mastery: float = 0.55
    target_retention: float = 0.8
    daily_new_limit: int = 10
    review_first: bool = False


_POLICY_TYPES = {"longterm": LongTermPolicy, "cram": CramPolicy}


def select_policy(days_left: int | None, override: str = "auto") -> StudyPolicy:
    """Choose a policy from exam distance, or from an explicit override.

    Args:
        days_left: whole days until the exam, or None when no date is set.
        override: ``auto`` (default), ``longterm`` or ``cram``.

    Raises:
        ValueError: if ``override`` is not a known mode.
    """
    if override not in ("auto", "", None):
        try:
            return _POLICY_TYPES[str(override)]()
        except KeyError as exc:
            raise ValueError("unknown mode %r (expected auto, longterm or cram)" % override) from exc
    if days_left is None:
        return LongTermPolicy()
    return LongTermPolicy() if days_left >= LONGTERM_THRESHOLD_DAYS else CramPolicy()


def policy_for_mode(mode: str) -> StudyPolicy:
    """Rebuild the policy object stored under a state's ``mode`` field."""
    try:
        return _POLICY_TYPES[mode]()
    except KeyError as exc:
        raise ValueError("unknown mode %r" % mode) from exc


POLICIES = {"longterm": LongTermPolicy, "cram": CramPolicy}
