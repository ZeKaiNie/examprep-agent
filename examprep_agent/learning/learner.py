# -*- coding: utf-8 -*-
"""Bayesian Knowledge Tracing (BKT) learner model.

Each knowledge point tracks ``p_known`` — the probability the learner knows it.
One answer event applies the standard four-parameter BKT update:

1. **posterior** given the observed answer, using ``p_guess`` (correct without
   knowing) and ``p_slip`` (wrong despite knowing);
2. **learning transition**, using ``p_learn`` (probability of learning after the
   opportunity);
3. clamp to a numerically safe interval.

The model is deliberately small but real: a correct answer always raises
``p_known`` and a wrong answer always lowers it for sensible parameters, and
repeated evidence converges toward 1 or 0.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_EPS = 1e-4


@dataclass(frozen=True)
class BKTParams:
    """The four canonical BKT parameters, each in (0, 1)."""

    p_init: float = 0.3    # prior probability the skill is known before any evidence
    p_learn: float = 0.15  # P(learn the skill after an opportunity)
    p_guess: float = 0.2   # P(correct | not known)
    p_slip: float = 0.1    # P(wrong | known)

    def __post_init__(self) -> None:
        for name in ("p_init", "p_learn", "p_guess", "p_slip"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError("%s must be in [0, 1], got %r" % (name, value))
        if self.p_guess >= 1.0 or self.p_slip >= 1.0:
            raise ValueError("p_guess and p_slip must be < 1")


@dataclass
class KnowledgeState:
    """Mutable per-knowledge-point estimate."""

    p_known: float
    attempts: int = 0
    correct: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize for study_state.json (full float precision, JSON round-trips exactly)."""
        return {"p_known": self.p_known, "attempts": self.attempts, "correct": self.correct}

    @classmethod
    def from_dict(cls, d: dict[str, Any], default: float = 0.3) -> KnowledgeState:
        """Rebuild from to_dict() output, tolerating missing fields."""
        return cls(
            p_known=float(d.get("p_known", default)),
            attempts=int(d.get("attempts", 0)),
            correct=int(d.get("correct", 0)),
        )


class KnowledgeStates(dict):
    """Per-KP states that materialise the BKT prior on first direct access.

    ``mastery()`` deliberately reads with ``.get`` and does *not* materialise,
    so a brand-new course still serialises ``kps == {}`` (setup --fresh keeps
    its empty-learner semantics).  Mutating code that needs a concrete state
    can index ``states[kp]`` and get one at ``p_init``.
    """

    def __init__(self, default: float = 0.3,
                 initial: Mapping[str, KnowledgeState] | None = None) -> None:
        super().__init__(initial or {})
        self.default = float(default)

    def __missing__(self, key: str) -> KnowledgeState:
        state = KnowledgeState(p_known=self.default)
        self[key] = state
        return state


class BKTModel:
    """A tiny BKT model holding one :class:`KnowledgeState` per knowledge point."""

    def __init__(self, params: BKTParams | None = None,
                 states: Mapping[str, KnowledgeState] | None = None) -> None:
        self.params = params or BKTParams()
        self.states: KnowledgeStates = KnowledgeStates(self.params.p_init, states)

    def get(self, kp: str) -> KnowledgeState:
        """Return (creating if needed) the state for a knowledge point."""
        return self.states[kp]

    def mastery(self, kp: str) -> float:
        """Current p(known) for a knowledge point (prior when unseen)."""
        state = self.states.get(kp)
        return state.p_known if state is not None else self.params.p_init

    def update(self, kp: str, correct: bool) -> KnowledgeState:
        """Apply one answer event and return the updated state."""
        state = self.get(kp)
        p = state.p_known
        guess, slip = self.params.p_guess, self.params.p_slip
        if correct:
            denominator = p * (1.0 - slip) + (1.0 - p) * guess
            posterior = (p * (1.0 - slip) / denominator) if denominator > 0 else p
        else:
            denominator = p * slip + (1.0 - p) * (1.0 - guess)
            posterior = (p * slip / denominator) if denominator > 0 else p
        learned = posterior + (1.0 - posterior) * self.params.p_learn
        state.p_known = min(1.0 - _EPS, max(_EPS, learned))
        state.attempts += 1
        if correct:
            state.correct += 1
        return state

    def observe(self, kp: str, correct: bool) -> float:
        """Convenience wrapper returning the updated mastery probability."""
        return self.update(kp, correct).p_known

    def mean_mastery(self, kps: list[str] | None = None) -> float:
        """Average mastery over ``kps`` (or every tracked point); prior when empty."""
        keys = list(self.states) if kps is None else [k for k in kps if k in self.states]
        if not keys:
            return self.params.p_init
        return sum(self.states[k].p_known for k in keys) / len(keys)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the whole model."""
        return {
            "params": {
                "p_init": self.params.p_init,
                "p_learn": self.params.p_learn,
                "p_guess": self.params.p_guess,
                "p_slip": self.params.p_slip,
            },
            "kps": {kp: state.to_dict() for kp, state in self.states.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> BKTModel:
        """Rebuild a model from to_dict() output (None yields a fresh model)."""
        if not data:
            return cls()
        params = BKTParams(**(data.get("params") or {}))
        states = {kp: KnowledgeState.from_dict(value) for kp, value in (data.get("kps") or {}).items()}
        return cls(params=params, states=states)
