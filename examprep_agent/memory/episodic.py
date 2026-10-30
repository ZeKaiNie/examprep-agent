# -*- coding: utf-8 -*-
"""Episodic memory: normalized append-only JSONL of study events.

Three event sources are normalized into ``<workspace>/memory/episodic.jsonl``:

- answer events (the authoritative source is ``study_state.json`` history);
- session/mode events;
- kernel agent-log events from ``<workspace>/logs/agent-*.jsonl`` (imported
  incrementally with a per-file line cursor).

Malformed or truncated lines are skipped rather than raising: a damaged tail
must never make the agent unusable.  Imports are idempotent (deduplicated by
event identity), so running them again does not grow duplicates.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from ..security.atomicio import atomic_write_json
from ..security.audit import read_jsonl, redact_secrets, redact_text

EPISODIC_FILE = "episodic.jsonl"
CURSOR_FILE = ".agent_log_cursor.json"
MAX_FIELD_CHARS = 600


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class EpisodicMemory:
    """Append-only event stream with defensive reads and idempotent imports."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, EPISODIC_FILE)
        self.cursor_path = os.path.join(self.root, CURSOR_FILE)
        self._damaged_lines = self._quarantine_damaged_lines()

    # -------------------------------------------------------------- integrity
    def _quarantine_damaged_lines(self) -> int:
        """Copy malformed/truncated lines into a ``.corrupt-*`` sidecar.

        The good events stay in place (append-only), but damage becomes visible
        evidence instead of being silently skipped on every read.  The sidecar
        name is content-addressed so repeated loads do not create duplicates.
        """
        if not os.path.exists(self.path):
            return 0
        damaged: list[str] = []
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        item = json.loads(stripped)
                    except (ValueError, TypeError):
                        damaged.append(stripped)
                        continue
                    if not isinstance(item, dict):
                        damaged.append(stripped)
        except OSError:
            return 0
        if damaged:
            digest = hashlib.sha256("\n".join(damaged).encode("utf-8")).hexdigest()[:12]
            sidecar = "%s.corrupt-%s" % (self.path, digest)
            if not os.path.exists(sidecar):
                try:
                    os.makedirs(self.root, exist_ok=True)
                    with open(sidecar, "w", encoding="utf-8") as fh:
                        fh.write("\n".join(damaged) + "\n")
                except OSError:
                    pass
        return len(damaged)

    @property
    def damaged_lines(self) -> int:
        """Malformed lines currently present in the event stream."""
        return self._damaged_lines

    # -------------------------------------------------------------- reading
    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Well-formed events (oldest first, optionally the last ``limit``)."""
        return read_jsonl(self.path, limit=limit)

    def count(self) -> int:
        """Number of well-formed events."""
        return len(read_jsonl(self.path))

    def _keys(self) -> set[tuple[str, ...]]:
        keys: set[tuple[str, ...]] = set()
        for event in read_jsonl(self.path):
            keys.add(_dedupe_key(event))
        return keys

    # -------------------------------------------------------------- writing
    def append(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Append one redacted event and return it."""
        entry: dict[str, Any] = {"ts": _utc_now(), "type": event_type}
        clean = redact_secrets(fields)
        for key, value in clean.items():
            if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
                value = value[:MAX_FIELD_CHARS] + "…"
            entry[str(key)] = value
        os.makedirs(self.root, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover - filesystem dependent
                pass
        return entry

    def _append_many(self, entries: Iterable[dict[str, Any]]) -> int:
        written = 0
        for entry in entries:
            self.append(str(entry.pop("type")), **entry)
            written += 1
        return written

    # -------------------------------------------------------------- imports
    def import_state_history(self, history: Iterable[Mapping[str, Any]]) -> int:
        """Normalize ``study_state.json`` history entries into episodic events."""
        existing = self._keys()
        pending: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            ts = str(item.get("ts") or _utc_now())
            chapter = item.get("chapter")
            event = {
                "ts": ts,
                "type": "answer",
                "qid": str(item.get("qid", "")),
                "result": str(item.get("result", "")),
                "chapter": chapter,
                "kp": ("ch%s" % chapter) if chapter is not None else None,
                "imported_from": "study_state",
            }
            if _dedupe_key(event) in existing:
                continue
            existing.add(_dedupe_key(event))
            pending.append(event)
        return self._append_many(pending)

    def ingest_agent_logs(self, logs_dir: str) -> int:
        """Normalize new lines of ``agent-*.jsonl`` into episodic events."""
        if not os.path.isdir(logs_dir):
            return 0
        cursor = self._read_cursor()
        existing_hashes = {
            str(event.get("line_hash"))
            for event in read_jsonl(self.path)
            if str(event.get("type", "")).startswith("agent_") and event.get("line_hash")
        }
        written = 0
        for name in sorted(os.listdir(logs_dir)):
            if not (name.startswith("agent-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(logs_dir, name)
            done = int(cursor.get(name, 0))
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            new_lines = lines[done:]
            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(item, dict):
                    continue
                event_name = str(item.get("event", "event"))
                payload = {
                    "ts": item.get("ts") or _utc_now(),
                    "type": "agent_%s" % event_name,
                    "session_id": item.get("session_id"),
                    "step": item.get("step"),
                    "source": name,
                    "line_hash": hashlib.sha256(line.encode("utf-8")).hexdigest()[:12],
                }
                if str(payload["line_hash"]) in existing_hashes:
                    continue
                existing_hashes.add(str(payload["line_hash"]))
                for key in ("tool", "ok", "gate", "error", "content"):
                    if key in item:
                        payload[key] = item[key]
                self.append("agent_%s" % event_name, **payload)
                written += 1
            cursor[name] = len(lines)
        self._write_cursor(cursor)
        return written

    def _read_cursor(self) -> dict[str, int]:
        try:
            with open(self.cursor_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return {str(k): int(v) for k, v in data.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _write_cursor(self, cursor: Mapping[str, int]) -> None:
        os.makedirs(self.root, exist_ok=True)
        atomic_write_json(self.cursor_path, dict(cursor))

    # -------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        """Counts by event type and the latest timestamp."""
        counts: dict[str, int] = {}
        latest = ""
        for event in read_jsonl(self.path):
            counts[str(event.get("type", "unknown"))] = counts.get(str(event.get("type", "unknown")), 0) + 1
            latest = max(latest, str(event.get("ts", "")))
        return {
            "events": sum(counts.values()),
            "by_type": counts,
            "latest": latest,
            "damaged_lines": self._damaged_lines,
        }

    def tail_text(self, limit: int = 20) -> str:
        """Short human-readable tail (redacted) for diagnostics."""
        lines = []
        for event in self.events(limit=limit):
            lines.append(redact_text(json.dumps(event, ensure_ascii=False, default=str)))
        return "\n".join(lines)


def _dedupe_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Identity of one event; ``result`` matters for same-minute answers.

    The state history timestamp used to have minute precision, so two answers
    to one question in the same minute would otherwise deduplicate to a single
    event and under-count the learner's history.
    """
    return (
        str(event.get("type", "")),
        str(event.get("qid") or event.get("line_hash") or event.get("session_id") or ""),
        str(event.get("ts", "")),
        str(event.get("result", "")),
    )
