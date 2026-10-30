# -*- coding: utf-8 -*-
"""Workspace-guarded file tools for the agent loop.

These are the only file primitives a model should ever see.  Each handler
re-checks the permission gate itself (defence in depth: calling a handler
directly cannot bypass the registry gate) and every delete is a move into
``.trash/``.
"""
from __future__ import annotations

import os
from typing import Any

from .atomicio import atomic_write_text
from .paths import PathPolicy, canonical
from .permissions import PermissionGate
from .risk import Access, Decision, RiskLevel
from .trash import TrashManager


class SecureFiles:
    """Permission-checked read/write/list/delete primitives."""

    def __init__(
        self,
        workspace: str,
        materials: str | None = None,
        *,
        gate: PermissionGate | None = None,
        trash: TrashManager | None = None,
    ) -> None:
        self.workspace = canonical(workspace)
        self.materials = canonical(materials) if materials else None
        self.policy = PathPolicy(workspace, materials)
        self.gate = gate or PermissionGate(workspace, materials)
        self.trash = trash or TrashManager(workspace, materials)
        self.snapshot_root = os.path.join(self.workspace, ".snapshots", "files")

    def _check(self, tool: str, path: str, access: Access, risk: RiskLevel, session_id: str = "") -> Decision:
        # Defense in depth: the handler re-checks the fence, but it must not
        # reserve a second write-throttle slot.  The registry gate (or the
        # caller) is the single throttle authority; otherwise a limit of N
        # would reject after N/2 real writes.
        decision = self.gate.check(
            tool, risk, {"path": path},
            access=access, session_id=session_id, count_throttle=False,
        )
        if not decision.allowed:
            raise PermissionError(decision.reason)
        return decision

    # -------------------------------------------------------------- tools
    def read_file(self, path: str, max_chars: int = 200_000) -> dict[str, Any]:
        """Read a text file inside the materials or workspace (or a granted path)."""
        self._check("read_file", path, Access.READ, RiskLevel.L0)
        target = self.policy.resolve(path)
        with open(target, encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_chars + 1)
        truncated = len(text) > max_chars
        return {"path": target, "content": text[:max_chars], "truncated": truncated}

    def write_file(self, path: str, content: str, append: bool = False) -> dict[str, Any]:
        """Atomically write/append text; the previous version is snapshotted."""
        self._check("write_file", path, Access.WRITE, RiskLevel.L1)
        target = self.policy.resolve(path)
        body = str(content)
        if append and os.path.exists(target):
            with open(target, encoding="utf-8", errors="replace") as fh:
                body = fh.read() + body
        atomic_write_text(target, body, snapshot_root=self.snapshot_root)
        return {"path": target, "bytes": len(body.encode("utf-8")), "appended": bool(append)}

    def list_dir(self, path: str = ".") -> dict[str, Any]:
        """List a directory (workspace/materials or granted path)."""
        self._check("list_dir", path, Access.READ, RiskLevel.L0)
        target = self.policy.resolve(path)
        entries: list[dict[str, Any]] = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({
                "name": name,
                "kind": "dir" if os.path.isdir(full) else "file",
                "size": os.path.getsize(full) if os.path.isfile(full) else None,
            })
        return {"path": target, "entries": entries}

    def make_dir(self, path: str) -> dict[str, Any]:
        """Create a directory inside the workspace (or a granted path)."""
        self._check("make_dir", path, Access.WRITE, RiskLevel.L1)
        target = self.policy.resolve(path)
        os.makedirs(target, exist_ok=True)
        return {"path": target, "created": True}

    def delete_path(self, path: str, reason: str = "") -> dict[str, Any]:
        """Move a file/directory into ``.trash/`` — never a true delete."""
        self._check("delete_path", path, Access.WRITE, RiskLevel.L1)
        record = self.trash.delete(self.policy.resolve(path), reason=reason or "tool delete")
        return record.to_dict()


def register_secure_file_tools(registry: Any, files: SecureFiles) -> Any:
    """Register the guarded file tools on a :class:`ToolRegistry`."""
    registry.register(
        "read_file",
        "Read a UTF-8 text file inside the course materials or study workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        files.read_file,
        risk=RiskLevel.L0,
        access="read",
        path_arguments=("path",),
    )
    registry.register(
        "write_file",
        "Atomically write a UTF-8 text file inside the study workspace.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "append": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
        files.write_file,
        risk=RiskLevel.L1,
        access="write",
        path_arguments=("path",),
    )
    registry.register(
        "list_dir",
        "List a directory inside the course materials or study workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        files.list_dir,
        risk=RiskLevel.L0,
        access="read",
        path_arguments=("path",),
    )
    registry.register(
        "make_dir",
        "Create a directory inside the study workspace.",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        files.make_dir,
        risk=RiskLevel.L1,
        access="write",
        path_arguments=("path",),
    )
    registry.register(
        "delete_path",
        "Move a file/directory into the workspace trash (never a hard delete).",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["path"],
        },
        files.delete_path,
        risk=RiskLevel.L1,
        access="write",
        path_arguments=("path",),
    )
    return registry
