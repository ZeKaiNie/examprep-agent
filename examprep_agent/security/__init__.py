# -*- coding: utf-8 -*-
"""P0.5 security layer: risk levels, path fence, grants, trash, audit, scanner.

Public entry points:

- :class:`RiskLevel` / :class:`Decision` — the four-level model from PLAN §5.7.
- :class:`PathPolicy` — materials read-only, workspace writable, realpath fence.
- :class:`PermissionStore` / :class:`PermissionGate` — persistent L2 grants
  (``permissions.json``), deterministic allow/deny decisions, write throttle.
- :class:`TrashManager` — move-to-``.trash`` deletion with retention markers.
- :class:`AuditLog` — redacting JSONL audit trail.
- :class:`DangerousCommandScanner` — static hard-block matcher.
- :class:`SecureFiles` — guarded file tools.
"""
from .atomicio import (
    atomic_write_json,
    atomic_write_text,
    latest_snapshot,
    snapshot_candidates,
    snapshot_file,
)
from .audit import AuditLog, audit_path, contains_secret, read_jsonl, redact_secrets, redact_text
from .commands import DangerousCommandError, DangerousCommandScanner, SecurityFinding, scan_text
from .filetools import SecureFiles, register_secure_file_tools
from .paths import (
    MATERIALS,
    OUTSIDE,
    PROTECTED_DIRS,
    PROTECTED_NAMES,
    WORKSPACE,
    PathPolicy,
    canonical,
    is_protected_path,
    is_within,
    path_key,
    same_path,
    validate_grant_target,
    validate_workspace,
)
from .permissions import (
    Grant,
    PermissionGate,
    PermissionStore,
    WriteThrottle,
    permissions_path,
)
from .risk import Access, Decision, RiskLevel
from .trash import TrashManager, TrashRecord

__all__ = [
    "Access",
    "AuditLog",
    "DangerousCommandError",
    "DangerousCommandScanner",
    "Decision",
    "Grant",
    "MATERIALS",
    "OUTSIDE",
    "PROTECTED_DIRS",
    "PROTECTED_NAMES",
    "PathPolicy",
    "PermissionGate",
    "PermissionStore",
    "RiskLevel",
    "SecureFiles",
    "SecurityFinding",
    "TrashManager",
    "TrashRecord",
    "WORKSPACE",
    "WriteThrottle",
    "atomic_write_json",
    "atomic_write_text",
    "audit_path",
    "canonical",
    "contains_secret",
    "is_protected_path",
    "is_within",
    "latest_snapshot",
    "path_key",
    "permissions_path",
    "read_jsonl",
    "redact_secrets",
    "redact_text",
    "register_secure_file_tools",
    "same_path",
    "scan_text",
    "snapshot_candidates",
    "snapshot_file",
    "validate_grant_target",
    "validate_workspace",
]
