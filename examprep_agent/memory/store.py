# -*- coding: utf-8 -*-
"""Three-layer long-term memory facade.

- :class:`~examprep_agent.memory.episodic.EpisodicMemory` — event stream;
- :class:`~examprep_agent.memory.semantic.SemanticMemory` — learner profile;
- :class:`~examprep_agent.memory.procedural.ProceduralMemory` — preferences and
  effective strategies.

All three live under ``<workspace>/memory/`` and are independently recoverable.
A corrupt layer is quarantined and replaced in memory by an empty default; the
other layers and the study loop keep working.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Mapping

from .episodic import EpisodicMemory
from .procedural import ProceduralMemory
from .semantic import SemanticMemory

MEMORY_DIR = "memory"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class LongTermMemory:
    """Facade over the episodic, semantic and procedural layers."""

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)
        self.root = os.path.join(self.workspace, MEMORY_DIR)
        self.episodic = EpisodicMemory(self.root)
        self.semantic = SemanticMemory(self.root)
        self.procedural = ProceduralMemory(self.root)

    # -------------------------------------------------------------- syncing
    def sync_from_state(self, state: Mapping[str, Any]) -> None:
        """Mirror stable preferences and import legacy history entries."""
        self.procedural.sync_preferences(state)
        raw_history = state.get("history")
        history = [item for item in (raw_history or []) if isinstance(item, Mapping)] \
            if isinstance(raw_history, (list, tuple)) else []
        raw_learner = state.get("learner")
        learner = raw_learner if isinstance(raw_learner, Mapping) else {}
        raw_kps = learner.get("kps")
        mastery_by_kp = {}
        if isinstance(raw_kps, Mapping):
            for kp, values in raw_kps.items():
                if isinstance(values, Mapping):
                    try:
                        mastery_by_kp[str(kp)] = float(values.get("p_known", 0.0))
                    except (TypeError, ValueError):
                        continue
        self.semantic.import_history(history, mastery_by_kp)
        self.episodic.import_state_history(history)

    # -------------------------------------------------------------- events
    def record_answer(
        self,
        *,
        qid: str,
        kp: str,
        result: str,
        mastery: float,
        chapter: int | None = None,
        note: str = "",
        ts: str | None = None,
    ) -> str:
        """Write one answer across all three layers; returns the error label."""
        stamp = ts or _utc_now()
        self.episodic.append(
            "answer",
            ts=stamp,
            qid=qid,
            kp=kp,
            result=result,
            mastery=mastery,
            chapter=chapter,
            note=note,
        )
        label = self.semantic.record_answer(
            kp=kp,
            qid=qid,
            correct=(result == "right"),
            note=note,
            mastery=mastery,
            chapter=chapter,
            ts=stamp,
            result=result,
        )
        self.procedural.record_answer(result)
        return label

    def record_explanation(self, chars: int = 0, strategy: str = "kernel_explanation") -> None:
        """Record a generated explanation in procedural memory."""
        self.procedural.record_explanation(strategy, chars=chars)
        self.episodic.append("explanation", strategy=strategy, chars=int(chars))

    def record_session_event(self, event: str, **fields: Any) -> None:
        """Record a generic session event (setup, mode switch, …)."""
        self.episodic.append(event, **fields)

    def ingest_agent_logs(self, logs_dir: str | None = None) -> int:
        """Normalize new kernel agent-log lines into episodic memory."""
        return self.episodic.ingest_agent_logs(logs_dir or os.path.join(self.workspace, "logs"))

    # -------------------------------------------------------------- views
    def summary(self) -> dict[str, Any]:
        """Snapshot of all three layers plus quarantine evidence."""
        return {
            "episodic": self.episodic.summary(),
            "semantic": self.semantic.summary(),
            "procedural": self.procedural.summary(),
            "corrupt_files": self.corrupt_files(),
        }

    def corrupt_files(self) -> list[str]:
        """Files quarantined after a parse failure (for diagnostics)."""
        if not os.path.isdir(self.root):
            return []
        return sorted(
            os.path.join(self.root, name)
            for name in os.listdir(self.root)
            if ".corrupt-" in name
        )
