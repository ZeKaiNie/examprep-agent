# -*- coding: utf-8 -*-
"""Durable study session: study state + learner model + review cards + checkpoint.

A :class:`Session` binds the deterministic ``tools.state`` study state to the
learning layer (BKT mastery and FSRS cards) and persists both:

- ``study_state.json`` — progress, history, mistakes, notes, learner state and
  review cards (via :mod:`examprep_agent.tools.state`, atomic writes);
- ``session.json`` — kernel checkpoint: mode, rolling context summary, branch
  metadata (reserved for conversation branching) and counters.

Both files are written through a temporary file, ``flush`` + ``os.fsync`` and
``os.replace``, so a crash never leaves a half-written JSON document.  Concurrent
writers to the same workspace are not coordinated (the fixed ``.tmp`` name is
inherited from the original skill); that is a P1 concern.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..learning.learner import BKTModel
from ..learning.modes import LongTermPolicy, StudyPolicy, policy_for_mode
from ..learning.scheduler import FSRSScheduler
from ..tools import state as st
from .context import ContextEngine

SESSION_FILE = "session.json"
CHECKPOINT_VERSION = 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class SessionCheckpoint:
    """Kernel-side checkpoint persisted as ``session.json``."""

    version: int = CHECKPOINT_VERSION
    workspace: str = ""
    mode: str = "longterm"
    context_summary: str = ""
    branch: dict[str, Any] = field(default_factory=dict)   # id/parent/label — branching is reserved
    counters: dict[str, int] = field(default_factory=dict)
    updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SessionCheckpoint:
        """Rebuild from JSON, ignoring unknown fields from newer versions."""
        if not data:
            return cls()
        return cls(
            version=int(data.get("version", CHECKPOINT_VERSION)),
            workspace=str(data.get("workspace", "")),
            mode=str(data.get("mode", "longterm")),
            context_summary=str(data.get("context_summary", "")),
            branch=dict(data.get("branch") or {}),
            counters={str(k): int(v) for k, v in (data.get("counters") or {}).items()},
            updated=str(data.get("updated", "")),
        )


class Session:
    """A loaded study workspace plus the in-memory kernel/learning state."""

    def __init__(self, workspace: str, state: dict[str, Any], checkpoint: SessionCheckpoint | None = None) -> None:
        self.workspace = os.path.abspath(workspace)
        self.state = state
        checkpoint = checkpoint or SessionCheckpoint(workspace=self.workspace)
        self.mode = str(state.get("mode") or checkpoint.mode or "longterm")
        try:
            self.policy: StudyPolicy = policy_for_mode(self.mode)
        except ValueError:
            self.policy = LongTermPolicy()
            self.mode = self.policy.name
        self.learner = BKTModel.from_dict(state.get("learner"))
        self.scheduler = FSRSScheduler.from_dict(state.get("reviews"))
        # The active mode owns the retention target: cram (0.8) must actually
        # change FSRS intervals rather than persisting a stale 0.9.
        self.scheduler.fsrs.desired_retention = float(self.policy.target_retention)
        self.context = ContextEngine(max_tokens=int(state.get("context_tokens", 8000)))
        self.context.summary = checkpoint.context_summary
        self.branch: dict[str, Any] = checkpoint.branch or {"id": "main", "parent": None, "label": "main"}
        self.counters: dict[str, int] = {"answers": 0, "explanations": 0, "compactions": 0}
        self.counters.update(checkpoint.counters)

    def apply_policy(self, policy: StudyPolicy) -> None:
        """Switch modes and re-wire the policy's FSRS retention target."""
        self.policy = policy
        self.mode = policy.name
        self.scheduler.fsrs.desired_retention = float(policy.target_retention)

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def load(cls, workspace: str) -> Session:
        """Load a workspace; raises FileNotFoundError when it was never set up."""
        state = st.load(workspace)
        if state is None:
            raise FileNotFoundError("no study state in %s" % workspace)
        checkpoint = cls._read_checkpoint(workspace)
        return cls(workspace, state, checkpoint)

    @classmethod
    def create(cls, workspace: str, state: dict[str, Any], mode: str = "longterm") -> Session:
        """Build a session for a freshly created study state."""
        state["mode"] = mode
        checkpoint = SessionCheckpoint(workspace=os.path.abspath(workspace), mode=mode)
        return cls(workspace, state, checkpoint)

    @classmethod
    def restore(cls, workspace: str) -> Session:
        """Alias for :meth:`load` — explicit checkpoint recovery entry point."""
        return cls.load(workspace)

    @classmethod
    def _read_checkpoint(cls, workspace: str) -> SessionCheckpoint:
        path = os.path.join(workspace, SESSION_FILE)
        if not os.path.exists(path):
            return SessionCheckpoint(workspace=os.path.abspath(workspace))
        try:
            with open(path, encoding="utf-8") as fh:
                return SessionCheckpoint.from_dict(json.load(fh))
        except (ValueError, TypeError, OSError):
            return SessionCheckpoint(workspace=os.path.abspath(workspace))

    def save(self) -> None:
        """Persist study state (atomic) and refresh the kernel checkpoint."""
        self.state["mode"] = self.mode
        self.state["learner"] = self.learner.to_dict()
        self.state["reviews"] = self.scheduler.to_dict()
        st.save(self.workspace, self.state)
        self.checkpoint()

    def checkpoint(self) -> SessionCheckpoint:
        """Atomically write ``session.json``."""
        cp = SessionCheckpoint(
            version=CHECKPOINT_VERSION,
            workspace=self.workspace,
            mode=self.mode,
            context_summary=self.context.summary,
            branch=self.branch,
            counters=self.counters,
            updated=_iso_now(),
        )
        _atomic_write_json(os.path.join(self.workspace, SESSION_FILE), cp.to_dict())
        return cp

    # ------------------------------------------------------------ branching
    def new_branch(self, label: str, parent: str | None = None) -> dict[str, Any]:
        """Reserve a new conversation branch (P0 stores metadata only)."""
        branch_id = "b%d" % (int(self.counters.get("branches", 0)) + 1)
        self.counters["branches"] = int(self.counters.get("branches", 0)) + 1
        self.branch = {
            "id": branch_id,
            "parent": parent if parent is not None else self.branch.get("id", "main"),
            "label": label,
            "created": _iso_now(),
        }
        return self.branch

    # ------------------------------------------------------------ learning events
    def record_answer(self, qid: str, chapter_n: int, result: str, note: str = "") -> dict[str, Any]:
        """Record a graded answer: study state + BKT + FSRS in one place.

        ``skip`` deliberately maps to FSRS Hard (rating 2): the card was reviewed
        but not recalled confidently, so it neither resets nor counts a lapse.
        """
        st.record_result(self.state, qid, chapter_n, result, note)
        kp = "ch%d" % chapter_n
        if result in ("right", "wrong"):
            self.learner.update(kp, result == "right")
        rating = {"right": 3, "wrong": 1, "skip": 2}[result]
        outcome = self.scheduler.review(qid, rating)
        self.counters["answers"] = int(self.counters.get("answers", 0)) + 1
        return {
            "kp": kp,
            "mastery": self.learner.mastery(kp),
            "due": outcome.card.due,
            "interval_days": outcome.interval_days,
        }

    def mastery(self, chapter_n: int) -> float:
        """BKT mastery for a chapter-level knowledge point."""
        return self.learner.mastery("ch%d" % chapter_n)

    def should_advance(self, chapter_n: int) -> bool:
        """Whether the active policy's mastery gate is satisfied for a chapter."""
        return self.policy.can_advance(self.mastery(chapter_n))

    def set_context_summary(self, summary: str) -> None:
        """Replace the rolling context summary without rewriting study state."""
        self.context.summary = summary
