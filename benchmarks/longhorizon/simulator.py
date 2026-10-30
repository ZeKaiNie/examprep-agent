# -*- coding: utf-8 -*-
"""Hidden simulated learner and the narrow API the agent is allowed to use.

Design rules (the integrity core of the long-horizon benchmark):

* the **true** memory state (per-item stability, last exposure, true recall
  probability) lives only in :class:`HiddenLearner`;
* the agent receives a process-isolated façade
  (:class:`benchmarks.longhorizon.remote.RemoteLearnerAPI`) with three methods
  (``teach``, ``quiz``, ``restudy``) and **no state attributes**; it observes a
  single boolean from ``quiz`` and never the probability behind it.  The
  :class:`HiddenLearner` is constructed only inside the child process, so no
  closure or object reachable from the agent can reference it;
* the evaluator reads aggregate retention through a separate pipe after the
  simulation.

Forgetting follows the public FSRS-4.5 retrievability curve
``R(t, S) = (1 + 19/81 · t/S)^(-0.5)``.  These are *simulated* memory
parameters, not a fitted model of any real person.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

FACTOR = 19.0 / 81.0
DECAY = -0.5


@dataclass
class SimulationParams:
    """Illustrative memory parameters (reported verbatim in the results)."""

    day_minutes: int = 30
    teach_minutes: int = 8
    quiz_minutes: int = 2
    restudy_minutes: int = 2
    initial_stability: float = 1.0
    retrieval_gain: float = 4.0
    retrieval_floor: float = 0.4
    failure_factor: float = 0.4
    reread_gain: float = 0.15
    reread_floor: float = 0.0
    max_stability: float = 60.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_minutes": self.day_minutes,
            "teach_minutes": self.teach_minutes,
            "quiz_minutes": self.quiz_minutes,
            "restudy_minutes": self.restudy_minutes,
            "initial_stability": self.initial_stability,
            "retrieval_gain": self.retrieval_gain,
            "retrieval_floor": self.retrieval_floor,
            "failure_factor": self.failure_factor,
            "reread_gain": self.reread_gain,
            "reread_floor": self.reread_floor,
            "max_stability": self.max_stability,
            "forgetting_curve": "FSRS-4.5: R(t,S) = (1 + 19/81 * t/S)^(-0.5)",
        }


@dataclass
class HiddenItem:
    """True memory state for one question; never exposed to the agent."""

    qid: str
    chapter: int
    stability: float = 0.0
    last_day: int | None = None
    seen: bool = False
    exposures: int = 0
    practices: int = 0
    successes: int = 0

    def recall(self, day: int) -> float:
        if not self.seen or self.last_day is None:
            return 0.0
        elapsed = max(0, int(day) - int(self.last_day))
        stability = max(0.1, self.stability)
        return (1.0 + FACTOR * elapsed / stability) ** DECAY


class HiddenLearner:
    """The simulator's ground truth.  The agent never receives this object."""

    def __init__(self, questions: Sequence[Mapping[str, Any]], seed: int,
                 params: SimulationParams | None = None) -> None:
        self.seed = int(seed)
        self.params = params or SimulationParams()
        self.items: dict[str, HiddenItem] = {}
        self._rngs: dict[str, random.Random] = {}
        self._test_rngs: dict[str, random.Random] = {}
        for question in questions:
            qid = str(question["id"])
            chapter = int(question.get("chapter") or 0)
            self.items[qid] = HiddenItem(qid=qid, chapter=chapter)
            self._rngs[qid] = random.Random("%d|practice|%s" % (self.seed, qid))
            self._test_rngs[qid] = random.Random("%d|test|%s" % (self.seed, qid))

    # ------------------------------------------------------------ learning events
    def teach(self, qids: Iterable[str], day: int) -> None:
        """Presentation/re-reading of a chapter: refresh and slightly strengthen memory."""
        for qid in qids:
            item = self.items[str(qid)]
            if not item.seen:
                item.seen = True
                item.stability = max(0.1, self.params.initial_stability)
            else:
                recall = item.recall(day)
                gain = self.params.reread_gain * (1.0 - recall) + self.params.reread_floor
                item.stability = min(self.params.max_stability, item.stability + gain)
            item.last_day = int(day)
            item.exposures += 1

    def restudy(self, qids: Iterable[str], day: int) -> None:
        """Massed restudy without a retrieval attempt (the baseline's action)."""
        self.teach(qids, day)

    def practice(self, qid: str, day: int) -> bool:
        """Retrieval practice: sample true recall, then update hidden stability."""
        item = self.items[str(qid)]
        if not item.seen:
            # A quiz before any presentation is still an exposure, but the
            # true recall probability is zero until the material is taught.
            item.seen = True
            item.stability = 0.1
            recall = 0.0
        else:
            recall = item.recall(day)
        correct = self._rngs[item.qid].random() < recall
        if correct:
            base = max(0.3, item.stability)
            item.stability = min(
                self.params.max_stability,
                base * (1.0 + self.params.retrieval_gain * (1.0 - recall))
                + self.params.retrieval_floor,
            )
        else:
            item.stability = max(0.25, item.stability * self.params.failure_factor)
        item.last_day = int(day)
        item.practices += 1
        if correct:
            item.successes += 1
        return correct

    # ------------------------------------------------------------ evaluation
    def expected_retention(self, day: int) -> float:
        """Mean true recall probability across all questions (0 for unseen)."""
        if not self.items:
            return 0.0
        return sum(item.recall(day) for item in self.items.values()) / len(self.items)

    def realized_test(self, day: int) -> float:
        """One sampled delayed test per item (separate RNG stream)."""
        if not self.items:
            return 0.0
        correct = sum(1 for item in self.items.values()
                      if item.seen and self._test_rngs[item.qid].random() < item.recall(day))
        return correct / len(self.items)

    def snapshot(self) -> dict[str, Any]:
        """Full hidden state, for isolation tests only (never written to results)."""
        return {
            "seed": self.seed,
            "items": {qid: {
                "stability": item.stability,
                "last_day": item.last_day,
                "seen": item.seen,
                "exposures": item.exposures,
                "practices": item.practices,
                "successes": item.successes,
            } for qid, item in self.items.items()},
        }


class LearnerAPI:
    """Protocol for the only learner-facing methods the agent may call.

    Concrete instances are :class:`benchmarks.longhorizon.remote.RemoteLearnerAPI`;
    the hidden learner itself is never captured by a closure here.
    """

    __slots__ = ()

    def teach(self, qids: Iterable[str], day: int) -> None:
        raise NotImplementedError

    def quiz(self, qid: str, day: int) -> bool:
        raise NotImplementedError

    def restudy(self, qids: Iterable[str], day: int) -> None:
        raise NotImplementedError

    def public_surface(self) -> tuple[str, ...]:
        return ("teach", "quiz", "restudy", "public_surface")
