# -*- coding: utf-8 -*-
"""Durable study session: state + learner model + graph + memory + checkpoint.

A :class:`Session` binds the deterministic ``tools.state`` study state to the
learning layer (BKT mastery, FSRS cards, prerequisite graph), the three-layer
long-term memory and the P0.5 security context:

- ``study_state.json`` — progress, history, mistakes, notes, learner state and
  review cards (atomic writes + pre-write snapshots);
- ``session.json`` — kernel checkpoint: mode, rolling context summary, branch
  metadata and counters;
- ``knowledge_graph.json`` — KP graph (nodes + prerequisite edges);
- ``memory/`` — episodic JSONL, semantic profile, procedural preferences;
- ``audit/audit-*.jsonl`` — security audit (read-only for tools); persistent
  grants live outside the workspace in the user config directory.

All writes are atomic and snapshotted; a corrupt checkpoint or memory layer
degrades to an empty default instead of crashing the session.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Sequence

from ..learning.graph import GRAPH_FILE, KnowledgeGraph, load_or_build
from ..learning.learner import BKTModel
from ..learning.modes import LongTermPolicy, StudyPolicy, policy_for_mode
from ..learning.planner import (
    DueReview,
    PrereqGap,
    Recommendation,
    daily_counters,
    due_kps,
    due_reviews,
    mark_new_chapter,
    recommend,
    unmet_prerequisites,
)
from ..learning.scheduler import FSRSScheduler
from ..memory.store import LongTermMemory
from ..security.atomicio import atomic_write_json, snapshot_candidates
from ..security.audit import AuditLog
from ..security.paths import PathPolicy
from ..security.permissions import PermissionGate, PermissionStore, WriteThrottle
from ..tools import state as st
from .context import ContextEngine

SESSION_FILE = "session.json"
CHECKPOINT_VERSION = 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: str, payload: dict[str, Any], snapshot_root: str | None = None) -> None:
    atomic_write_json(path, payload, snapshot_root=snapshot_root)


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
        try:
            self.learner = BKTModel.from_dict(state.get("learner"))
        except (ValueError, TypeError, AttributeError, KeyError):
            # A hand-edited/corrupt learner block must not make the whole
            # workspace unusable; the graph and history remain authoritative.
            self.learner = BKTModel()
        try:
            self.scheduler = FSRSScheduler.from_dict(state.get("reviews"))
        except (ValueError, TypeError, AttributeError, KeyError):
            self.scheduler = FSRSScheduler()
        # The active mode owns the retention target: cram (0.8) must actually
        # change FSRS intervals rather than persisting a stale 0.9.
        self.scheduler.fsrs.desired_retention = float(self.policy.target_retention)
        self.context = ContextEngine(max_tokens=int(state.get("context_tokens", 8000)))
        self.context.summary = checkpoint.context_summary
        self.branch: dict[str, Any] = checkpoint.branch or {"id": "main", "parent": None, "label": "main"}
        self.counters: dict[str, int] = {"answers": 0, "explanations": 0, "compactions": 0}
        self.counters.update(checkpoint.counters)

        # ---- P0.5 security context (paths, grants, audit, write throttle)
        self.materials = state.get("materials")
        self.path_policy = PathPolicy(self.workspace, self.materials)
        self.audit = AuditLog(self.workspace)
        self.permissions = PermissionStore(self.workspace)
        self.throttle = WriteThrottle(self.workspace)
        self.gate = PermissionGate(
            self.workspace,
            self.materials,
            store=self.permissions,
            audit=self.audit,
            throttle=self.throttle,
        )

        # ---- P1 learner model: graph + long-term memory
        try:
            self.graph: KnowledgeGraph = load_or_build(self.workspace, state)
        except (ValueError, TypeError, OSError):  # corrupt graph must not block study
            from ..learning.graph import graph_from_state

            try:
                self.graph = graph_from_state(state)
            except (ValueError, TypeError, AttributeError, KeyError):
                self.graph = KnowledgeGraph()
        self.memory = LongTermMemory(self.workspace)
        try:
            self.memory.sync_from_state(state)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # Memory is a side record: a damaged layer degrades, it never breaks
            # loading or the answer flow.
            self.log_memory_error("sync", exc)

    def log_memory_error(self, layer: str, exc: BaseException) -> None:
        """Best-effort audit of a memory failure; never raises into the caller."""
        try:
            self.audit.log("memory_write_error", layer=layer, error=str(exc))
        except OSError:
            pass

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
            for snapshot in snapshot_candidates(path, os.path.join(workspace, ".snapshots")):
                try:
                    with open(snapshot, encoding="utf-8") as fh:
                        return SessionCheckpoint.from_dict(json.load(fh))
                except (ValueError, TypeError, OSError):
                    continue
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
        _atomic_write_json(
            os.path.join(self.workspace, SESSION_FILE),
            cp.to_dict(),
            snapshot_root=os.path.join(self.workspace, ".snapshots"),
        )
        return cp

    def save_graph(self) -> None:
        """Atomically persist ``knowledge_graph.json``."""
        self.graph.save(os.path.join(self.workspace, GRAPH_FILE))

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
    def record_answer(self, qid: str, chapter_n: int, result: str, note: str = "",
                      today: date | None = None) -> dict[str, Any]:
        """Record a graded answer: study state + BKT + FSRS + long-term memory.

        ``skip`` deliberately maps to FSRS Hard (rating 2): the card was reviewed
        but not recalled confidently, so it neither resets nor counts a lapse.

        ``today`` lets an offline simulation drive the scheduler on a simulated
        clock without changing production behaviour (defaults to the real date).
        """
        st.record_result(self.state, qid, chapter_n, result, note)
        kp = "ch%d" % chapter_n
        if result in ("right", "wrong"):
            self.learner.update(kp, result == "right")
        rating = {"right": 3, "wrong": 1, "skip": 2}[result]
        outcome = self.scheduler.review(qid, rating, today=today, kp=kp)
        self.counters["answers"] = int(self.counters.get("answers", 0)) + 1
        daily_counters(self.state, today)["answers"] += 1
        mastery = self.learner.mastery(kp)
        state_ts = ""
        if self.state.get("history"):
            state_ts = str(self.state["history"][-1].get("ts", ""))
        try:
            self.memory.record_answer(
                qid=qid,
                kp=kp,
                result=result,
                mastery=mastery,
                chapter=chapter_n,
                note=note,
                ts=state_ts or None,
            )
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # Memory must never break the study loop: a schema/IO failure is
            # recorded and the answer itself is already persisted in state.
            self.log_memory_error("long_term", exc)
        return {
            "kp": kp,
            "mastery": mastery,
            "due": outcome.card.due,
            "interval_days": outcome.interval_days,
        }

    def mastery(self, chapter_n: int) -> float:
        """BKT mastery for a chapter-level knowledge point."""
        return self.learner.mastery("ch%d" % chapter_n)

    def mastery_of(self, kp: str) -> float:
        """BKT mastery for an arbitrary KP id (``chN`` today)."""
        return self.learner.mastery(kp)

    def should_advance(self, chapter_n: int) -> bool:
        """Whether the active policy's mastery gate is satisfied for a chapter."""
        return self.policy.can_advance(self.mastery(chapter_n))

    def set_context_summary(self, summary: str) -> None:
        """Replace the rolling context summary without rewriting study state."""
        self.context.summary = summary

    def apply_policy(self, policy: StudyPolicy) -> None:
        """Switch modes and re-wire the policy's FSRS retention target."""
        self.policy = policy
        self.mode = policy.name
        self.state["mode"] = policy.name
        self.scheduler.fsrs.desired_retention = float(policy.target_retention)
        try:
            self.memory.procedural.sync_preferences(self.state)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            self.log_memory_error("procedural", exc)

    # ------------------------------------------------------------ planner views
    def due_reviews(self, bank: Sequence[dict[str, Any]] | None = None,
                    today: date | None = None) -> list[DueReview]:
        """FSRS cards due today, enriched with question/KP metadata."""
        return due_reviews(self, bank or [], today)

    def due_kps(self, bank: Sequence[dict[str, Any]] | None = None,
                today: date | None = None) -> set[str]:
        """Knowledge points with at least one due card."""
        return due_kps(self, bank or [], today)

    def predicted_retention(self, today: date | None = None) -> float | None:
        """Mean predicted recall across reviewed cards (None when none exist)."""
        return self.scheduler.predicted_retention(today)

    def graph_stats(self, bank: Sequence[dict[str, Any]] | None = None,
                    today: date | None = None) -> dict[str, Any]:
        """Unlocked/blocked/due counts for the status panel."""
        return self.graph.statistics(
            self.learner.mastery,
            float(self.policy.advance_mastery),
            self.due_kps(bank, today),
        )

    def unmet_prerequisites(self, chapter_n: int) -> list[PrereqGap]:
        """Blocking prerequisite gaps for a chapter."""
        return unmet_prerequisites(self, chapter_n)

    def recommend(self, bank: Sequence[dict[str, Any]] | None = None,
                  today: date | None = None) -> Recommendation:
        """The planner's single next action."""
        return recommend(self, bank or [], today)

    def mark_new_chapter(self, chapter_n: int, today: date | None = None) -> bool:
        """Count a new chapter against today's policy budget."""
        return mark_new_chapter(self.state, chapter_n, today)

    def daily(self, today: date | None = None) -> dict[str, Any]:
        """Today's counters (new chapters, reviews, answers)."""
        return daily_counters(self.state, today)
