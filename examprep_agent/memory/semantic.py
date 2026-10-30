# -*- coding: utf-8 -*-
"""Semantic memory: the learner profile (mastery curves + error patterns).

Stored as ``<workspace>/memory/semantic.json``:

``mastery_history``
    Per-KP curve of BKT ``p_known`` values, one point per graded answer.
``error_patterns``
    Clusters of wrong answers by deterministic note heuristics:
    ``concept_confusion``, ``calculation_error``, ``careless``,
    ``knowledge_gap`` or ``unclassified`` — plus a per-KP breakdown.
``recent_errors``
    Bounded, redacted examples so the tutor can reference concrete mistakes.

A corrupt file is quarantined and replaced by an empty profile at read time;
the agent keeps working instead of crashing.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from ..security.atomicio import atomic_write_json, snapshot_candidates
from ..security.audit import redact_text

SEMANTIC_FILE = "semantic.json"
SEMANTIC_VERSION = 1
MAX_HISTORY_PER_KP = 500
MAX_RECENT_ERRORS = 100

ERROR_LABELS = ("concept_confusion", "calculation_error", "careless", "knowledge_gap", "unclassified")

_KEYWORDS = {
    "concept_confusion": (
        "混淆", "概念", "定义", "区别", "理解错", "弄混", "搞混", "分不清",
        "confus", "concept", "definition", "misunderst", "mix up", "mixed up",
    ),
    "calculation_error": (
        "计算", "算错", "算术", "符号错", "推导错", "公式错", "加减",
        "calculation", "arithmetic", "compute", "sign error", "wrong formula",
    ),
    "careless": (
        "粗心", "大意", "看错", "抄错", "笔误", "漏看", "手滑",
        "careless", "silly", "typo", "misread", "slip",
    ),
    "knowledge_gap": (
        "不会", "没学过", "忘了", "忘记", "没见过", "不知道", "没掌握",
        "forgot", "don't know", "unknown", "never learned", "gap",
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def classify_error(note: str, result: str = "wrong") -> str:
    """Deterministic error cluster from a learner/tutor note."""
    if result not in ("wrong", "skip"):
        return ""
    text = (note or "").lower()
    for label in ("concept_confusion", "calculation_error", "careless", "knowledge_gap"):
        if any(keyword in text for keyword in _KEYWORDS[label]):
            return label
    return "unclassified"


def _empty_profile() -> dict[str, Any]:
    return {
        "version": SEMANTIC_VERSION,
        "updated": "",
        "answers": 0,
        "correct": 0,
        "mastery_history": {},
        "error_patterns": {label: 0 for label in ERROR_LABELS},
        "kp_error_patterns": {},
        "recent_errors": [],
    }


class SemanticMemory:
    """Learner profile with defensive persistence."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, SEMANTIC_FILE)
        self.data = self._load()

    # -------------------------------------------------------------- loading
    def _load(self) -> dict[str, Any]:
        """Parse the profile, falling back to a snapshot, then to empty defaults.

        A syntactically valid JSON document with corrupted *types* (for example
        ``"answers": "abc"`` or ``"kp_error_patterns": []``) is corruption too:
        it is quarantined and the newest good snapshot is restored instead of
        letting a ``ValueError``/``AttributeError`` reach the CLI.
        """
        if not os.path.exists(self.path):
            return _empty_profile()
        try:
            return self._parse(self._read_json(self.path))
        except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
            self._quarantine()
        for snapshot in snapshot_candidates(self.path, self.snapshot_root):
            try:
                return self._parse(self._read_json(snapshot))
            except (OSError, ValueError, TypeError, AttributeError, KeyError, OverflowError):
                continue
        return _empty_profile()

    @property
    def snapshot_root(self) -> str:
        """Directory holding pre-write snapshots of this layer."""
        return os.path.join(self.root, ".snapshots")

    @staticmethod
    def _read_json(path: str) -> Any:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _count(value: Any) -> int:
        count = int(value)
        if count < 0:
            raise ValueError("negative counter")
        return count

    @classmethod
    def _parse(cls, data: Any) -> dict[str, Any]:
        """Validate every field and return a clean profile (raises on damage)."""
        if not isinstance(data, dict):
            raise ValueError("semantic memory is not an object")
        profile = _empty_profile()
        profile["version"] = cls._count(data.get("version", SEMANTIC_VERSION))
        profile["updated"] = str(data.get("updated", ""))
        profile["answers"] = cls._count(data.get("answers", 0))
        profile["correct"] = cls._count(data.get("correct", 0))

        raw_history = data.get("mastery_history")
        if raw_history is None:
            raw_history = {}
        if not isinstance(raw_history, dict):
            raise ValueError("mastery_history must be an object")
        history: dict[str, list[dict[str, Any]]] = {}
        for kp, points in raw_history.items():
            if not isinstance(points, list):
                raise ValueError("mastery_history[%r] must be a list" % kp)
            clean_points: list[dict[str, Any]] = []
            for point in points:
                if not isinstance(point, dict):
                    raise ValueError("mastery_history point must be an object")
                p_known = point.get("p_known")
                if p_known is not None:
                    p_known = float(p_known)
                    if not math.isfinite(p_known):
                        raise ValueError("non-finite mastery point")
                chapter = point.get("chapter")
                if chapter is not None:
                    chapter = int(chapter)
                clean_points.append({
                    "ts": str(point.get("ts", "")),
                    "p_known": p_known,
                    "correct": bool(point.get("correct", False)),
                    "result": str(point.get("result", "right" if point.get("correct") else "wrong")),
                    "qid": str(point.get("qid", "")),
                    "chapter": chapter,
                })
            history[str(kp)] = clean_points[-MAX_HISTORY_PER_KP:]
        profile["mastery_history"] = history

        raw_patterns = data.get("error_patterns")
        if raw_patterns is None:
            raw_patterns = {}
        if not isinstance(raw_patterns, dict):
            raise ValueError("error_patterns must be an object")
        profile["error_patterns"] = {
            label: cls._count(raw_patterns.get(label, 0)) for label in ERROR_LABELS
        }

        raw_kp_patterns = data.get("kp_error_patterns")
        if raw_kp_patterns is None:
            raw_kp_patterns = {}
        if not isinstance(raw_kp_patterns, dict):
            raise ValueError("kp_error_patterns must be an object")
        kp_patterns: dict[str, dict[str, int]] = {}
        for kp, values in raw_kp_patterns.items():
            if not isinstance(values, dict):
                raise ValueError("kp_error_patterns[%r] must be an object" % kp)
            kp_patterns[str(kp)] = {str(label): cls._count(count) for label, count in values.items()}
        profile["kp_error_patterns"] = kp_patterns

        raw_recent = data.get("recent_errors")
        if raw_recent is None:
            raw_recent = []
        if not isinstance(raw_recent, list):
            raise ValueError("recent_errors must be a list")
        profile["recent_errors"] = [
            dict(item) for item in raw_recent if isinstance(item, dict)
        ][-MAX_RECENT_ERRORS:]
        return profile

    def _quarantine(self) -> None:
        try:
            target = "%s.corrupt-%d-%d" % (self.path, int(datetime.now().timestamp()), os.getpid())
            os.replace(self.path, target)
        except OSError:
            pass

    # -------------------------------------------------------------- writing
    def save(self) -> None:
        """Atomically persist the profile."""
        self.data["updated"] = _utc_now()
        os.makedirs(self.root, exist_ok=True)
        atomic_write_json(self.path, self.data, snapshot_root=self.snapshot_root)

    def record_answer(
        self,
        kp: str,
        qid: str,
        correct: bool,
        note: str = "",
        mastery: float | None = None,
        chapter: int | None = None,
        ts: str | None = None,
        result: str | None = None,
        save: bool = True,
    ) -> str:
        """Append a mastery point and, for a wrong answer, an error cluster."""
        stamp = ts or _utc_now()
        outcome = result or ("right" if correct else "wrong")
        self.data["answers"] = int(self.data.get("answers", 0)) + 1
        if correct:
            self.data["correct"] = int(self.data.get("correct", 0)) + 1
        point = {
            "ts": stamp,
            "p_known": float(mastery) if mastery is not None else None,
            "correct": bool(correct),
            "result": outcome,
            "qid": qid,
            "chapter": chapter,
        }
        history = self.data.setdefault("mastery_history", {}).setdefault(kp, [])
        history.append(point)
        if len(history) > MAX_HISTORY_PER_KP:
            del history[:-MAX_HISTORY_PER_KP]
        label = ""
        if not correct:
            label = classify_error(note, "wrong")
            self.data["error_patterns"][label] = int(self.data["error_patterns"].get(label, 0)) + 1
            kp_patterns = self.data.setdefault("kp_error_patterns", {}).setdefault(kp, {})
            kp_patterns[label] = int(kp_patterns.get(label, 0)) + 1
            recent = self.data.setdefault("recent_errors", [])
            recent.append({
                "ts": stamp,
                "kp": kp,
                "qid": qid,
                "label": label,
                "note": redact_text(str(note or ""))[:240],
            })
            if len(recent) > MAX_RECENT_ERRORS:
                del recent[:-MAX_RECENT_ERRORS]
        if save:
            self.save()
        return label

    def _seen(self) -> set[tuple[str, str, str]]:
        return {
            (
                str(point.get("qid", "")),
                str(point.get("ts", "")),
                str(point.get("result") or ("right" if point.get("correct") else "wrong")),
            )
            for points in self.data.get("mastery_history", {}).values()
            for point in points
        }

    def import_history(
        self,
        history: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        mastery_by_kp: dict[str, float] | None = None,
    ) -> int:
        """Replay legacy ``study_state.json`` history into the profile once.

        Entries are deduplicated by ``(qid, ts, result)``: two answers to the
        same question inside one timestamped minute must not collapse into one
        event (that under-counted both mastery and error clusters).
        """
        mastery_by_kp = mastery_by_kp or {}
        seen = self._seen()
        imported = 0
        for item in history:
            if not isinstance(item, Mapping):
                continue
            qid = str(item.get("qid", ""))
            ts = str(item.get("ts") or "")
            result = str(item.get("result") or "")
            key = (qid, ts, result)
            if key in seen:
                continue
            seen.add(key)
            chapter = item.get("chapter")
            try:
                chapter_n = int(chapter) if chapter is not None else None
            except (TypeError, ValueError):
                chapter_n = None
            kp = "ch%s" % chapter_n if chapter_n is not None else "unknown"
            self.record_answer(
                kp=kp,
                qid=qid,
                correct=result == "right",
                note="",
                mastery=mastery_by_kp.get(kp),
                chapter=chapter_n,
                ts=ts or None,
                result=result or None,
                save=False,
            )
            imported += 1
        if imported:
            self.save()
        return imported

    # -------------------------------------------------------------- views
    def history(self, kp: str) -> list[dict[str, Any]]:
        """Mastery curve for one KP (possibly empty)."""
        return list(self.data.get("mastery_history", {}).get(kp, []))

    def error_profile(self) -> dict[str, Any]:
        """Global error cluster counts plus the per-KP breakdown."""
        return {
            "overall": dict(self.data.get("error_patterns", {})),
            "by_kp": {kp: dict(values) for kp, values in self.data.get("kp_error_patterns", {}).items()},
            "recent": list(self.data.get("recent_errors", [])),
        }

    def summary(self) -> dict[str, Any]:
        """Compact status view."""
        answers = int(self.data.get("answers", 0))
        correct = int(self.data.get("correct", 0))
        return {
            "answers": answers,
            "correct": correct,
            "accuracy": (correct / answers) if answers else None,
            "kps": len(self.data.get("mastery_history", {})),
            "error_patterns": dict(self.data.get("error_patterns", {})),
        }
