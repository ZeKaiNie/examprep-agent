# -*- coding: utf-8 -*-
"""Procedural memory: stable preferences and which study strategies work.

Stored as ``<workspace>/memory/procedural.json``:

- ``preferences`` — language, profile (small/standard/large), mode, slice size
  and the derived explanation granularity (the tutor's default verbosity);
- ``strategies`` — usage/success counters for the teaching moves actually used
  (quiz, review, kernel explanation), with a computed accuracy;
- ``mode_history`` — mode switches with timestamps.

This is the layer a future session reads to avoid re-asking the learner how
they like to study.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from ..security.atomicio import atomic_write_json, snapshot_candidates

PROCEDURAL_FILE = "procedural.json"
PROCEDURAL_VERSION = 1
MAX_MODE_HISTORY = 50

_GRANULARITY = {"small": "concise", "standard": "standard", "large": "detailed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _empty_profile() -> dict[str, Any]:
    return {
        "version": PROCEDURAL_VERSION,
        "updated": "",
        "preferences": {
            "profile": "standard",
            "language": "zh",
            "mode": "longterm",
            "slice_chars": 3000,
            "explanation_granularity": "standard",
        },
        "strategies": {},
        "mode_history": [],
        "explanation_events": 0,
        "answers": 0,
        "correct": 0,
    }


class ProceduralMemory:
    """Preference/strategy store with defensive persistence."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, PROCEDURAL_FILE)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        """Parse preferences, falling back to a snapshot, then to empty defaults."""
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
            raise ValueError("procedural memory is not an object")
        profile = _empty_profile()
        profile["version"] = cls._count(data.get("version", PROCEDURAL_VERSION))
        profile["updated"] = str(data.get("updated", ""))
        profile["explanation_events"] = cls._count(data.get("explanation_events", 0))
        profile["answers"] = cls._count(data.get("answers", 0))
        profile["correct"] = cls._count(data.get("correct", 0))

        raw_prefs = data.get("preferences")
        if raw_prefs is None:
            raw_prefs = {}
        if not isinstance(raw_prefs, dict):
            raise ValueError("preferences must be an object")
        defaults = _empty_profile()["preferences"]
        prefs = dict(defaults)
        prefs.update({key: value for key, value in raw_prefs.items() if key in prefs})
        # Coerce to the expected scalar shapes: a list/dict here would later
        # blow up in ``_GRANULARITY.get`` or state mirroring.
        profile_name = str(prefs.get("profile", "standard"))
        prefs["profile"] = profile_name if profile_name in _GRANULARITY else "standard"
        language = prefs.get("language")
        prefs["language"] = language if isinstance(language, str) and language.strip() \
            else defaults["language"]
        mode = prefs.get("mode")
        prefs["mode"] = mode if isinstance(mode, str) and mode.strip() else defaults["mode"]
        try:
            prefs["slice_chars"] = int(prefs.get("slice_chars", defaults["slice_chars"]))
        except (TypeError, ValueError):
            prefs["slice_chars"] = defaults["slice_chars"]
        prefs["explanation_granularity"] = str(
            prefs.get("explanation_granularity", defaults["explanation_granularity"]))
        profile["preferences"] = prefs

        raw_strategies = data.get("strategies")
        if raw_strategies is None:
            raw_strategies = {}
        if not isinstance(raw_strategies, dict):
            raise ValueError("strategies must be an object")
        strategies: dict[str, dict[str, int]] = {}
        for name, values in raw_strategies.items():
            if not isinstance(values, dict):
                raise ValueError("strategies[%r] must be an object" % name)
            strategies[str(name)] = {
                "uses": cls._count(values.get("uses", 0)),
                "successes": cls._count(values.get("successes", 0)),
            }
            if values.get("last_chars") is not None:
                strategies[str(name)]["last_chars"] = cls._count(values.get("last_chars"))
        profile["strategies"] = strategies

        raw_history = data.get("mode_history")
        if raw_history is None:
            raw_history = []
        if not isinstance(raw_history, list):
            raise ValueError("mode_history must be a list")
        profile["mode_history"] = [
            dict(item) for item in raw_history if isinstance(item, dict)
        ][-MAX_MODE_HISTORY:]
        return profile

    def _quarantine(self) -> None:
        try:
            target = "%s.corrupt-%d-%d" % (self.path, int(datetime.now().timestamp()), os.getpid())
            os.replace(self.path, target)
        except OSError:
            pass

    def save(self) -> None:
        """Atomically persist the profile."""
        self.data["updated"] = _utc_now()
        os.makedirs(self.root, exist_ok=True)
        atomic_write_json(self.path, self.data, snapshot_root=self.snapshot_root)

    # -------------------------------------------------------------- updates
    def sync_preferences(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Mirror stable preferences from ``study_state.json``."""
        prefs = self.data.setdefault("preferences", _empty_profile()["preferences"])
        changed = False
        mapping = {
            "profile": state.get("profile", prefs.get("profile", "standard")),
            "language": state.get("language", prefs.get("language", "zh")),
            "mode": state.get("mode", prefs.get("mode", "longterm")),
            "slice_chars": state.get("slice_chars", prefs.get("slice_chars", 3000)),
        }
        for key, value in mapping.items():
            if value is not None and prefs.get(key) != value:
                prefs[key] = value
                changed = True
        profile_name = str(prefs.get("profile", "standard"))
        granularity = _GRANULARITY.get(profile_name, "standard")
        if prefs.get("explanation_granularity") != granularity:
            prefs["explanation_granularity"] = granularity
            changed = True
        mode = str(prefs.get("mode", "longterm"))
        history = self.data.setdefault("mode_history", [])
        if not history or history[-1].get("mode") != mode:
            history.append({"mode": mode, "ts": _utc_now()})
            if len(history) > MAX_MODE_HISTORY:
                del history[:-MAX_MODE_HISTORY]
            changed = True
        if changed:
            self.save()
        return dict(prefs)

    def record_answer(self, result: str, strategy: str = "quiz") -> None:
        """Count a graded attempt and its strategy success."""
        self.data["answers"] = int(self.data.get("answers", 0)) + 1
        record = self.data.setdefault("strategies", {}).setdefault(strategy, {"uses": 0, "successes": 0})
        record["uses"] = int(record.get("uses", 0)) + 1
        if result == "right":
            record["successes"] = int(record.get("successes", 0)) + 1
            self.data["correct"] = int(self.data.get("correct", 0)) + 1
        self.save()

    def record_explanation(self, strategy: str = "kernel_explanation", chars: int = 0) -> None:
        """Count one generated explanation (and its approximate size)."""
        self.data["explanation_events"] = int(self.data.get("explanation_events", 0)) + 1
        record = self.data.setdefault("strategies", {}).setdefault(strategy, {"uses": 0, "successes": 0})
        record["uses"] = int(record.get("uses", 0)) + 1
        if chars:
            record["last_chars"] = int(chars)
        self.save()

    def record_strategy(self, name: str, success: bool) -> None:
        """Record an arbitrary strategy outcome (used by future tutoring modes)."""
        record = self.data.setdefault("strategies", {}).setdefault(name, {"uses": 0, "successes": 0})
        record["uses"] = int(record.get("uses", 0)) + 1
        if success:
            record["successes"] = int(record.get("successes", 0)) + 1
        self.save()

    # -------------------------------------------------------------- views
    def preferences(self) -> dict[str, Any]:
        """Stable preference view."""
        return dict(self.data.get("preferences", {}))

    def effective_strategies(self) -> list[dict[str, Any]]:
        """Strategies with at least one use, ranked by empirical success rate."""
        out = []
        for name, values in self.data.get("strategies", {}).items():
            uses = int(values.get("uses", 0))
            successes = int(values.get("successes", 0))
            if not uses:
                continue
            out.append({
                "name": name,
                "uses": uses,
                "successes": successes,
                "success_rate": successes / uses,
            })
        out.sort(key=lambda item: (-item["success_rate"], -item["uses"], item["name"]))
        return out

    def summary(self) -> dict[str, Any]:
        """Compact status view."""
        return {
            "preferences": self.preferences(),
            "strategies": self.effective_strategies(),
            "explanation_events": int(self.data.get("explanation_events", 0)),
        }
