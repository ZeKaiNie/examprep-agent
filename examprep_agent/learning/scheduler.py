# -*- coding: utf-8 -*-
"""FSRS-style spaced-repetition scheduling.

This is a compact, real implementation of the public FSRS-4.5 formulas (not a
constant stub): each card carries difficulty ``D``, stability ``S`` and an
implicit retrievability ``R(t, S)``; a review updates ``D``/``S`` and the next
interval is derived from the requested retention target.  The :class:`Scheduler`
protocol is what the teaching loop depends on, so a future FSRS-5/6 or custom
scheduler can drop in without touching the CLI.

Reference: https://github.com/open-spaced-repetition/fsrs4anki (FSRS-4.5).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from enum import IntEnum
from typing import Any, Protocol

# FSRS-4.5 default weights (open-spaced-repetition/fsrs4anki).
DEFAULT_WEIGHTS: tuple[float, ...] = (
    0.4872, 1.4003, 3.7145, 13.8206,   # w0..w3  initial stability by rating
    5.1618, 1.2298,                    # w4..w5  initial difficulty
    0.8975, 0.031,                     # w6..w7  difficulty update / mean reversion
    1.6474, 0.1367, 1.0461,            # w8..w10 stability increase
    2.1072, 0.0793, 0.3246, 1.587,     # w11..w14 forgetting stability
    0.2272, 2.8755,                    # w15..w16 hard penalty / easy bonus
)

DECAY = -0.5
FACTOR = 19.0 / 81.0


class Rating(IntEnum):
    """Review grades in the FSRS convention."""

    AGAIN = 1
    HARD = 2
    GOOD = 3
    EASY = 4


@dataclass
class Card:
    """One review card (P0 uses question ids as card keys)."""

    key: str
    due: str                        # ISO date
    stability: float
    difficulty: float
    reps: int = 0
    lapses: int = 0
    last_review: str | None = None
    state: str = "new"              # new | review | relearning
    kp: str | None = None           # knowledge point this card belongs to

    def to_dict(self) -> dict[str, Any]:
        """Serialize for study_state.json."""
        return {
            "key": self.key,
            "due": self.due,
            "stability": round(self.stability, 6),
            "difficulty": round(self.difficulty, 6),
            "reps": self.reps,
            "lapses": self.lapses,
            "last_review": self.last_review,
            "state": self.state,
            "kp": self.kp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Card:
        """Rebuild from to_dict() output."""
        return cls(
            key=str(d["key"]),
            due=str(d["due"]),
            stability=float(d["stability"]),
            difficulty=float(d["difficulty"]),
            reps=int(d.get("reps", 0)),
            lapses=int(d.get("lapses", 0)),
            last_review=d.get("last_review"),
            state=str(d.get("state", "new")),
            kp=d.get("kp"),
        )


@dataclass(frozen=True)
class ReviewOutcome:
    """Result of applying one review."""

    card: Card
    interval_days: int
    retrievability: float


class Scheduler(Protocol):
    """Interface the teaching loop uses for spaced repetition."""

    def review(self, key: str, rating: int, today: date | None = None) -> ReviewOutcome:
        """Apply a review to a card (creating it when new)."""
        ...

    def due(self, today: date | None = None) -> list[Card]:
        """Cards whose due date is on/before ``today``."""
        ...


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class FSRS:
    """FSRS-4.5 formulas over ``Card`` values (pure, deterministic)."""

    def __init__(self, weights: tuple[float, ...] = DEFAULT_WEIGHTS, desired_retention: float = 0.9) -> None:
        if len(weights) < 17:
            raise ValueError("FSRS requires at least 17 weights")
        if not 0.0 < desired_retention < 1.0:
            raise ValueError("desired_retention must be in (0, 1)")
        self.w = weights
        self.desired_retention = desired_retention

    # -------------------------------------------------------------- formulas
    def initial_stability(self, rating: int) -> float:
        """S0(rating): stability right after the first exposure."""
        return max(0.1, self.w[int(rating) - 1])

    def initial_difficulty(self, rating: int) -> float:
        """D0(rating): difficulty right after the first exposure."""
        return _clamp(self.w[4] - (int(rating) - 3) * self.w[5], 1.0, 10.0)

    def retrievability(self, elapsed_days: float, stability: float) -> float:
        """R(t, S) = (1 + FACTOR·t/S)^DECAY — probability of recall at time t."""
        stability = max(0.1, stability)
        return (1.0 + FACTOR * max(0.0, elapsed_days) / stability) ** DECAY

    def next_interval(self, stability: float, desired_retention: float | None = None) -> int:
        """Days until the next review at the requested retention (at least 1)."""
        retention = self.desired_retention if desired_retention is None else desired_retention
        raw = stability / FACTOR * (retention ** (1.0 / DECAY) - 1.0)
        return max(1, int(round(raw)))

    def next_difficulty(self, difficulty: float, rating: int) -> float:
        """Update difficulty for a non-first review, with mean reversion."""
        delta = -self.w[6] * (int(rating) - 3)
        updated = difficulty + delta
        reverted = self.w[7] * self.initial_difficulty(4) + (1.0 - self.w[7]) * updated
        return _clamp(reverted, 1.0, 10.0)

    def next_stability(self, card: Card, rating: int, elapsed_days: float) -> float:
        """Update stability after a review at ``elapsed_days`` since the last one."""
        rating = int(rating)
        stability = max(0.1, card.stability)
        difficulty = _clamp(card.difficulty, 1.0, 10.0)
        retrievability = self.retrievability(elapsed_days, stability)
        if rating == Rating.AGAIN:
            value = (
                self.w[11]
                * difficulty ** (-self.w[12])
                * ((stability + 1.0) ** self.w[13] - 1.0)
                * math.exp(self.w[14] * (1.0 - retrievability))
            )
            return max(0.1, value)
        hard_penalty = self.w[15] if rating == Rating.HARD else 1.0
        easy_bonus = self.w[16] if rating == Rating.EASY else 1.0
        growth = (
            math.exp(self.w[8])
            * (11.0 - difficulty)
            * stability ** (-self.w[9])
            * (math.exp((1.0 - retrievability) * self.w[10]) - 1.0)
            * hard_penalty
            * easy_bonus
        )
        return max(0.1, stability * (1.0 + growth))

    # -------------------------------------------------------------- review
    def review(self, card: Card, rating: int, today: date | None = None) -> ReviewOutcome:
        """Apply one rating to ``card``, returning an updated copy and interval."""
        rating = int(rating)
        if rating not in (1, 2, 3, 4):
            raise ValueError("rating must be 1..4")
        today = today or date.today()
        if card.reps == 0:
            stability = self.initial_stability(rating)
            difficulty = self.initial_difficulty(rating)
            retrievability = 0.0
            lapses = 1 if rating == Rating.AGAIN else 0
            state = "relearning" if rating == Rating.AGAIN else "review"
        else:
            elapsed = 0.0
            if card.last_review:
                elapsed = max(0.0, float((today - date.fromisoformat(card.last_review)).days))
            retrievability = self.retrievability(elapsed, card.stability)
            stability = self.next_stability(card, rating, elapsed)
            difficulty = self.next_difficulty(card.difficulty, rating)
            lapses = card.lapses + (1 if rating == Rating.AGAIN else 0)
            state = "relearning" if rating == Rating.AGAIN else "review"
        interval = self.next_interval(stability)
        updated = Card(
            key=card.key,
            due=(today + timedelta(days=interval)).isoformat(),
            stability=stability,
            difficulty=difficulty,
            reps=card.reps + 1,
            lapses=lapses,
            last_review=today.isoformat(),
            state=state,
            kp=card.kp,
        )
        return ReviewOutcome(card=updated, interval_days=interval, retrievability=retrievability)


class FSRSScheduler:
    """Card store built on :class:`FSRS` (the P0 default scheduler)."""

    def __init__(self, fsrs: FSRS | None = None, cards: dict[str, Card] | None = None) -> None:
        self.fsrs = fsrs or FSRS()
        self.cards: dict[str, Card] = dict(cards or {})

    def get(self, key: str) -> Card | None:
        """Return a card if it exists."""
        return self.cards.get(key)

    def get_or_create(self, key: str, today: date | None = None) -> Card:
        """Return the card for ``key``, creating a new one due today."""
        card = self.cards.get(key)
        if card is None:
            today = today or date.today()
            card = Card(key=key, due=today.isoformat(), stability=0.0, difficulty=0.0, state="new")
            self.cards[key] = card
        return card

    def review(self, key: str, rating: int, today: date | None = None, kp: str | None = None) -> ReviewOutcome:
        """Review a card by key, creating it on first exposure.

        ``kp`` tags the card with its knowledge point so the planner can map due
        reviews back to the mastery graph even when the question bank is absent.
        """
        today = today or date.today()
        card = self.get_or_create(key, today)
        if kp is not None:
            card.kp = kp
        outcome = self.fsrs.review(card, rating, today)
        self.cards[key] = outcome.card
        return outcome

    def due(self, today: date | None = None) -> list[Card]:
        """Cards due on/before ``today``, earliest first."""
        today = today or date.today()
        items = [c for c in self.cards.values() if c.due <= today.isoformat()]
        return sorted(items, key=lambda c: (c.due, c.key))

    def due_count(self, today: date | None = None) -> int:
        """Number of cards due on/before ``today``."""
        return len(self.due(today))

    def retrievability(self, card: Card, today: date | None = None) -> float:
        """Predicted recall probability for one card right now (0 for unseen)."""
        if not card.reps or not card.last_review:
            return 0.0
        today = today or date.today()
        try:
            elapsed = max(0.0, float((today - date.fromisoformat(card.last_review)).days))
        except ValueError:
            return 0.0
        return self.fsrs.retrievability(elapsed, card.stability)

    def predicted_retention(self, today: date | None = None) -> float | None:
        """Mean predicted recall over reviewed cards (None when there are none)."""
        values = [
            self.retrievability(card, today)
            for card in self.cards.values()
            if card.reps and card.last_review
        ]
        if not values:
            return None
        return sum(values) / len(values)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the scheduler state, including the FSRS weights in use."""
        return {
            "desired_retention": self.fsrs.desired_retention,
            "weights": list(self.fsrs.w),
            "cards": {key: card.to_dict() for key, card in self.cards.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FSRSScheduler:
        """Rebuild from to_dict() output (None yields an empty store).

        Raises:
            ValueError: when stored weights are present but fewer than 17.
        """
        if not data:
            return cls()
        raw_weights = data.get("weights")
        weights = DEFAULT_WEIGHTS if raw_weights is None else tuple(float(w) for w in raw_weights)
        if len(weights) < 17:
            raise ValueError("FSRS requires at least 17 weights")
        fsrs = FSRS(weights=weights, desired_retention=float(data.get("desired_retention", 0.9)))
        cards = {key: Card.from_dict(value) for key, value in (data.get("cards") or {}).items()}
        return cls(fsrs=fsrs, cards=cards)
