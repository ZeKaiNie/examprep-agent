# -*- coding: utf-8 -*-
"""Workspace trash: every deletion is a move plus a retention marker.

Nothing in the agent deletes user files for real.  :meth:`TrashManager.delete`
moves the target under ``<workspace>/.trash/<stamp>-<id>/`` and appends a record
with the original path, deletion time and ``retain_until`` date to
``.trash/index.json``.  The record and the file can be restored later; the
retention date is informational (the engine never performs a hard delete).
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .atomicio import atomic_write_json
from .audit import redact_text
from .paths import MATERIALS, PathPolicy, canonical, is_within, same_path

TRASH_DIR = ".trash"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TrashRecord:
    """One trashed item."""

    id: str
    original: str
    stored: str
    deleted_at: str
    retain_until: str
    reason: str = ""
    kind: str = "file"
    status: str = "trashed"
    restored_to: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON view for .trash/index.json."""
        return {
            "id": self.id,
            "original": self.original,
            "stored": self.stored,
            "deleted_at": self.deleted_at,
            "retain_until": self.retain_until,
            "reason": self.reason,
            "kind": self.kind,
            "status": self.status,
            "restored_to": self.restored_to,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrashRecord":
        """Rebuild from index JSON, tolerating older records."""
        return cls(
            id=str(data["id"]),
            original=str(data["original"]),
            stored=str(data["stored"]),
            deleted_at=str(data.get("deleted_at", "")),
            retain_until=str(data.get("retain_until", "")),
            reason=str(data.get("reason", "")),
            kind=str(data.get("kind", "file")),
            status=str(data.get("status", "trashed")),
            restored_to=data.get("restored_to"),
        )


class TrashManager:
    """Move-to-trash implementation with a JSON index."""

    def __init__(
        self,
        workspace: str,
        materials: str | None = None,
        *,
        retain_days: int = 30,
    ) -> None:
        self.workspace = canonical(workspace)
        self.materials = canonical(materials) if materials else None
        self.policy = PathPolicy(workspace, materials)
        self.retain_days = max(0, int(retain_days))
        self.root = os.path.join(self.workspace, TRASH_DIR)
        self.index_path = os.path.join(self.root, "index.json")
        self.records: list[TrashRecord] = self._read()

    def _read(self) -> list[TrashRecord]:
        if not os.path.exists(self.index_path):
            return []
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, TypeError):
            try:
                os.replace(self.index_path, "%s.corrupt-%d" % (self.index_path, int(datetime.now().timestamp())))
            except OSError:
                pass
            return []
        raw = data.get("records") if isinstance(data, dict) else data
        records: list[TrashRecord] = []
        for item in raw or []:
            try:
                records.append(TrashRecord.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _save(self) -> None:
        atomic_write_json(self.index_path, {"version": 1, "records": [r.to_dict() for r in self.records]})

    def _next_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = "t-%s" % stamp
        counter = 0
        while any(record.id == candidate for record in self.records):
            counter += 1
            candidate = "t-%s-%d" % (stamp, counter)
        return candidate

    def delete(self, path: str, reason: str = "") -> TrashRecord:
        """Move ``path`` into the workspace trash and record its retention date."""
        target = canonical(path)
        if same_path(target, self.workspace) or (
            self.materials is not None and same_path(target, self.materials)
        ):
            raise ValueError("refusing to trash a workspace/materials root")
        # Case/Unicode variants and ancestors must not slip past the root guard.
        if is_within(self.workspace, target) or (
            self.materials is not None and is_within(self.materials, target)
        ):
            raise ValueError("refusing to trash an ancestor of the workspace/materials")
        if same_path(target, self.root) or is_within(target, self.root):
            raise ValueError("refusing to trash the trash itself")
        # The default workspace lives inside the materials folder; only the
        # material area *outside* the workspace is read-only.
        if self.policy.locate(target) == MATERIALS:
            raise PermissionError("course materials are read-only")
        if not os.path.exists(target):
            raise FileNotFoundError(target)
        record_id = self._next_id()
        container = os.path.join(self.root, record_id)
        os.makedirs(container, exist_ok=True)
        stored = os.path.join(container, os.path.basename(target) or "item")
        counter = 0
        while os.path.exists(stored):
            counter += 1
            stored = os.path.join(container, "%s.%d" % (os.path.basename(target), counter))
        shutil.move(target, stored)
        deleted_at = _utc_now()
        retain_until = (datetime.now(timezone.utc) + timedelta(days=self.retain_days)).date().isoformat()
        record = TrashRecord(
            id=record_id,
            original=target,
            stored=stored,
            deleted_at=deleted_at,
            retain_until=retain_until,
            reason=redact_text(str(reason or "")),
            kind="dir" if os.path.isdir(stored) else "file",
        )
        self.records.append(record)
        self._save()
        return record

    def get(self, record_id: str) -> TrashRecord | None:
        """Find one trashed item."""
        return next((record for record in self.records if record.id == record_id), None)

    def list(self) -> list[TrashRecord]:
        """All trash records, oldest first."""
        return list(self.records)

    def restore(self, record_id: str, destination: str | None = None) -> str:
        """Move a trashed item back (default: its original path); returns the path."""
        record = self.get(record_id)
        if record is None:
            raise KeyError(record_id)
        if not os.path.exists(record.stored):
            raise FileNotFoundError(record.stored)
        target = canonical(destination) if destination else record.original
        # Restores are host-side moves; an explicit destination must still obey
        # the fence (never into read-only materials, never outside the workspace
        # unless it is the recorded original path).
        if self.policy.locate(target) == MATERIALS:
            raise PermissionError("course materials are read-only")
        if destination is not None and not self.policy.is_workspace(target):
            raise PermissionError("restore destination must be inside the workspace")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        candidate = target
        counter = 0
        while os.path.exists(candidate):
            counter += 1
            candidate = "%s.restored-%d" % (target, counter)
        shutil.move(record.stored, candidate)
        record.status = "restored"
        record.restored_to = candidate
        self._save()
        return candidate
