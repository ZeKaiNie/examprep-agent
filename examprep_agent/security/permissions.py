# -*- coding: utf-8 -*-
"""Persistent L2 grants, the permission gate and the write circuit breaker.

Grants live **outside the model-writable workspace**, in the user's config
directory (``$EXAMPREP_CONFIG_DIR`` or ``$XDG_CONFIG_HOME/examprep``, one file
per workspace hash).  A workspace file therefore can never become an
authorization root: even if a model could overwrite ``permissions.json`` in the
workspace, that legacy file is ignored.  Grants are scoped either to a
directory (covers every descendant, checked on ``realpath``) or to a capability
such as ``network``.  They are created explicitly by the user
(``examprep permissions grant …``), never by the model, and can be revoked at
any time.  The gate never reads a risk level from tool arguments: risk is a
property of the registration, and the effective risk can only be raised by the
path fence, never lowered by the caller.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .audit import AuditLog, contains_secret
from .atomicio import atomic_write_json
from .paths import (
    MATERIALS,
    OUTSIDE,
    WORKSPACE,
    PathPolicy,
    canonical,
    is_protected_path,
    is_within,
    validate_grant_target,
)
from .risk import Access, Decision, RiskLevel

PERMISSIONS_FILE = "permissions.json"
GRANT_VERSION = 1
_PATH_KEYS = frozenset(
    {"path", "file", "filename", "filepath", "dir", "directory", "target",
     "destination", "dest", "source", "src", "cwd", "workdir", "out", "output",
     "paths", "files", "dirs", "directories", "targets", "destinations",
     "sources", "outputs", "inputs", "file_path", "output_path", "input_path"}
)
_PATH_SUFFIXES = ("_path", "_paths", "_file", "_files", "_dir", "_dirs",
                  "_directory", "_directories", "_filename", "_folder", "_folders")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config_root() -> str:
    """Directory for user-owned security state (never inside the workspace)."""
    explicit = os.environ.get("EXAMPREP_CONFIG_DIR")
    if explicit:
        return canonical(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return canonical(os.path.join(base, "examprep"))


def permissions_path(workspace: str) -> str:
    """Per-workspace grant file in the user's config directory.

    The workspace path is hashed so two courses cannot read each other's
    grants, and the file lives outside the L1 (model-writable) region.
    """
    digest = hashlib.sha256(canonical(workspace).encode("utf-8")).hexdigest()[:16]
    return os.path.join(config_root(), "permissions", "%s.json" % digest)


def _normalise_access(access: Iterable[str] | str) -> list[str]:
    if isinstance(access, str):
        values = [access]
    else:
        values = list(access)
    clean = sorted({str(value).strip().lower() for value in values if str(value).strip()})
    for value in clean:
        if value not in {"read", "write", "network", "execute"}:
            raise ValueError("unknown access %r" % value)
    return clean


@dataclass
class Grant:
    """One remembered L2 authorization."""

    id: str
    kind: str                      # directory | capability
    value: str
    access: list[str]
    created: str = field(default_factory=_utc_now)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON view for permissions.json."""
        return {
            "id": self.id,
            "kind": self.kind,
            "value": self.value,
            "access": list(self.access),
            "created": self.created,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], workspace: str | None = None) -> "Grant":
        """Rebuild one grant, tolerating older/hand-edited files.

        Relative directory values are resolved against ``workspace`` (never the
        process cwd, which would make the same file mean different things
        depending on where the CLI was started).
        """
        kind = str(data.get("kind", ""))
        value = str(data.get("value", ""))
        if kind not in ("directory", "capability") or not value:
            raise ValueError("invalid grant")
        if kind == "directory":
            value = value if os.path.isabs(os.path.expanduser(value)) else os.path.join(
                canonical(workspace or os.getcwd()), value)
            value = canonical(value)
        return cls(
            id=str(data.get("id") or _grant_id(kind, value, str(data.get("created", "")))),
            kind=kind,
            value=value,
            access=_normalise_access(data.get("access") or ("write",)),
            created=str(data.get("created") or _utc_now()),
            note=redact_note(str(data.get("note", ""))),
        )


def redact_note(note: str) -> str:
    """Persisted grant notes must not carry credentials."""
    from .audit import redact_text

    return redact_text(note)


def _grant_id(kind: str, value: str, created: str) -> str:
    digest = hashlib.sha256(("%s|%s|%s" % (kind, value, created)).encode("utf-8")).hexdigest()
    return "g-" + digest[:10]


class PermissionStore:
    """Read/write the user-config grant table for one workspace.

    The grant file is deliberately **not** under ``<workspace>``: the workspace
    is the L1 region a model's file tools may write, so nothing there may be
    trusted as an authorization root.  A legacy ``<workspace>/permissions.json``
    is ignored (and is read-only through the gate).
    """

    def __init__(self, workspace: str, path: str | None = None) -> None:
        self.workspace = canonical(workspace)
        self.path = os.path.abspath(path) if path else permissions_path(workspace)
        if is_within(self.path, self.workspace):
            # Fail closed: a grant file inside the model-writable region could
            # be forged by the model, which would defeat A1 entirely.
            raise ValueError("permission store must live outside the workspace")
        self.legacy_path = os.path.join(self.workspace, PERMISSIONS_FILE)
        self.grants: list[Grant] = self._read()

    # -------------------------------------------------------------- loading
    def _read(self) -> list[Grant]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, TypeError):
            self._quarantine()
            return []
        raw = data.get("grants") if isinstance(data, dict) else data
        grants: list[Grant] = []
        for item in raw or []:
            try:
                grants.append(Grant.from_dict(item, workspace=self.workspace))
            except (ValueError, TypeError, AttributeError):
                continue
        return grants

    def _quarantine(self) -> None:
        """Keep a corrupt permissions file as evidence instead of deleting it."""
        try:
            target = "%s.corrupt-%d-%d" % (self.path, int(time.time()), os.getpid())
            os.replace(self.path, target)
        except OSError:
            pass

    def _save(self) -> None:
        atomic_write_json(self.path, {
            "version": GRANT_VERSION,
            "updated": _utc_now(),
            "grants": [g.to_dict() for g in self.grants],
        })

    # -------------------------------------------------------------- queries
    def list(self) -> list[Grant]:
        """All grants, oldest first."""
        return list(self.grants)

    def get(self, grant_id: str) -> Grant | None:
        """Find a grant by id."""
        return next((g for g in self.grants if g.id == grant_id), None)

    def find_directory(self, path: str, access: str) -> Grant | None:
        """Most specific directory grant covering ``path`` with ``access``."""
        target = canonical(path)
        matches = [
            grant for grant in self.grants
            if grant.kind == "directory" and access in grant.access and is_within(target, grant.value)
        ]
        matches.sort(key=lambda g: len(g.value), reverse=True)
        return matches[0] if matches else None

    def find_capability(self, capability: str, access: str = "network") -> Grant | None:
        """Capability grant (e.g. ``network``) with the requested access."""
        return next(
            (g for g in self.grants
             if g.kind == "capability" and g.value == capability and access in g.access),
            None,
        )

    # -------------------------------------------------------------- mutation
    def grant_directory(self, path: str, access: Iterable[str] | str = ("read", "write"), note: str = "") -> Grant:
        """Remember a directory authorization (idempotent for the same scope)."""
        value = canonical(path)
        validate_grant_target(value)
        access_list = _normalise_access(access)
        note = redact_note(note)
        for grant in self.grants:
            if grant.kind == "directory" and grant.value == value:
                grant.access = sorted(set(grant.access) | set(access_list))
                grant.note = note or grant.note
                self._save()
                return grant
        created = _utc_now()
        grant = Grant(id=_grant_id("directory", value, created), kind="directory",
                      value=value, access=access_list, created=created, note=note)
        self.grants.append(grant)
        self._save()
        return grant

    def grant_capability(self, capability: str, access: Iterable[str] | str = ("network",), note: str = "") -> Grant:
        """Remember a capability authorization (currently ``network``)."""
        access_list = _normalise_access(access)
        note = redact_note(note)
        for grant in self.grants:
            if grant.kind == "capability" and grant.value == capability:
                grant.access = sorted(set(grant.access) | set(access_list))
                grant.note = note or grant.note
                self._save()
                return grant
        created = _utc_now()
        grant = Grant(id=_grant_id("capability", capability, created), kind="capability",
                      value=capability, access=access_list, created=created, note=note)
        self.grants.append(grant)
        self._save()
        return grant

    def revoke(self, grant_id: str) -> bool:
        """Remove a grant by id; returns whether it existed."""
        before = len(self.grants)
        self.grants = [g for g in self.grants if g.id != grant_id]
        if len(self.grants) != before:
            self._save()
            return True
        return False


class WriteThrottle:
    """Sliding-window write circuit breaker persisted per workspace.

    When more than ``limit`` write operations happen within ``window_seconds``,
    further writes are denied with ``write_throttle`` until the window slides.
    The counter survives CLI invocations because it is stored under
    ``<workspace>/.security/write_window.json`` (an internal path that the
    model's file tools cannot modify).

    Scope: this breaker covers **tool-mediated** writes, which is the surface a
    model controls.  Engine-internal writes (``study_state.json``, memory,
    setup output) are trusted host operations executed with atomic replacement
    and pre-write snapshots; they deliberately do not consume model quota.
    """

    def __init__(self, workspace: str, limit: int = 30, window_seconds: float = 60.0) -> None:
        self.workspace = canonical(workspace)
        self.limit = max(1, int(limit))
        self.window_seconds = max(0.1, float(window_seconds))
        self.path = os.path.join(self.workspace, ".security", "write_window.json")
        self._timestamps: list[float] = self._read()

    def _read(self) -> list[float]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            values = data.get("timestamps", []) if isinstance(data, dict) else []
            return sorted(float(value) for value in values if isinstance(value, (int, float)))
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        atomic_write_json(self.path, {"timestamps": self._timestamps[-self.limit * 4:]})

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._timestamps = [value for value in self._timestamps if value > cutoff]

    def allow(self, now: float | None = None) -> tuple[bool, int]:
        """Reserve one write slot.

        Returns ``(allowed, used)`` where ``used`` is the number of writes in the
        current window after reservation.
        """
        current = time.time() if now is None else float(now)
        self._prune(current)
        if len(self._timestamps) >= self.limit:
            return False, len(self._timestamps)
        self._timestamps.append(current)
        self._save()
        return True, len(self._timestamps)

    def reset(self) -> None:
        """Clear the rolling window."""
        self._timestamps = []
        self._save()


class PermissionGate:
    """Deterministic allow/deny policy applied before any tool handler runs."""

    def __init__(
        self,
        workspace: str,
        materials: str | None = None,
        *,
        store: PermissionStore | None = None,
        audit: AuditLog | None = None,
        throttle: WriteThrottle | None = None,
        session_id: str = "",
    ) -> None:
        self.workspace = canonical(workspace)
        self.policy = PathPolicy(workspace, materials)
        self.store = store or PermissionStore(workspace)
        # A gate without an audit trail would silently weaken A5.  Callers may
        # still pass their own log; otherwise create the standard per-workspace
        # one.  Nothing is written until the first decision.
        self.audit = audit if audit is not None else AuditLog(self.workspace)
        self.throttle = throttle
        self.session_id = session_id

    # -------------------------------------------------------------- helpers
    def _record(self, decision: Decision, session_id: str = "") -> Decision:
        self.audit.decision(decision, session_id=session_id or self.session_id)
        return decision

    @staticmethod
    def _flatten_strings(value: Any, found: list[str]) -> None:
        if isinstance(value, str):
            if value.strip():
                found.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                PermissionGate._flatten_strings(item, found)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                PermissionGate._flatten_strings(item, found)

    @staticmethod
    def _looks_like_path_key(key: str) -> bool:
        lowered = str(key).strip().lower()
        return lowered in _PATH_KEYS or lowered.endswith(_PATH_SUFFIXES)

    def _path_arguments(
        self,
        arguments: Mapping[str, Any],
        declared: Sequence[str] | None = None,
    ) -> list[str]:
        """Collect path-valued arguments.

        Tools should declare ``path_arguments`` at registration.  Independent
        of that declaration the gate also walks the argument tree and treats
        common path keys (``path``, ``output_path``, ``file_path``, ``paths``,
        nested ``options.path`` …) as paths, so a tool cannot dodge the fence
        by renaming an argument.
        """
        found: list[str] = []
        if declared:
            for key in declared:
                if key in arguments:
                    self._flatten_strings(arguments[key], found)
                else:  # accept case-insensitive declarations too
                    for actual, value in arguments.items():
                        if str(actual).lower() == str(key).lower():
                            self._flatten_strings(value, found)

        def walk(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if self._looks_like_path_key(str(key)):
                        self._flatten_strings(item, found)
                    elif isinstance(item, Mapping):
                        walk(item)
                    elif isinstance(item, (list, tuple)):
                        for element in item:
                            if isinstance(element, Mapping):
                                walk(element)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, Mapping):
                        walk(item)

        walk(arguments)
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        unique: list[str] = []
        for item in found:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    @staticmethod
    def _command_text(arguments: Mapping[str, Any]) -> str:
        """Raw command-like argument values (string values are not JSON-quoted)."""
        pieces: list[str] = []
        for key, value in arguments.items():
            if str(key).lower() not in {"command", "cmd", "script", "shell", "code"}:
                continue
            if isinstance(value, str):
                pieces.append(value)
            else:
                pieces.append(json.dumps(value, ensure_ascii=False, default=str))
        return "\n".join(pieces)

    def _hint_grant(self, path: str) -> str:
        return "examprep permissions grant %s" % path

    # -------------------------------------------------------------- checks
    def check(
        self,
        tool: str,
        risk: RiskLevel | int | str,
        arguments: Mapping[str, Any] | None = None,
        *,
        access: Access | str | None = None,
        capability: str | None = None,
        session_id: str = "",
        path_arguments: Sequence[str] | None = None,
        count_throttle: bool = True,
    ) -> Decision:
        """Evaluate one tool call.  Safe by default: anything unknown is denied."""
        arguments = dict(arguments or {})
        level = RiskLevel.parse(risk)
        if access is None:
            mode = Access.READ if level == RiskLevel.L0 else Access.WRITE
        elif isinstance(access, Access):
            mode = access
        else:
            mode = Access(str(access).lower())

        if contains_secret(arguments):
            return self._record(Decision(
                allowed=False,
                risk=level,
                code="secret_in_arguments",
                reason="credential material in tool arguments is never sent or written",
                tool=tool,
                access=mode.value,
            ), session_id)

        raw_paths = self._path_arguments(arguments, path_arguments)
        resolved = [self.policy.resolve(raw) for raw in raw_paths]
        if mode in (Access.WRITE, Access.EXECUTE):
            for target in resolved:
                if is_protected_path(self.workspace, target):
                    return self._record(Decision(
                        allowed=False,
                        risk=level,
                        code="internal_path_read_only",
                        reason="engine-owned control path is read-only for tools: %s" % target,
                        tool=tool,
                        access=mode.value,
                        path=target,
                        details={"protected": True},
                    ), session_id)

        command = self._command_text(arguments)
        findings: list[Any] = []
        if command and (mode == Access.EXECUTE or level == RiskLevel.L3):
            try:
                from .commands import DangerousCommandScanner
                findings = DangerousCommandScanner().scan(command, source="tool:%s" % tool)
            except Exception:  # pragma: no cover - scanner must never break the gate
                findings = []
        if findings:
            return self._record(Decision(
                allowed=False,
                risk=level,
                code="dangerous_command",
                reason="destructive command pattern blocked by deterministic policy",
                tool=tool,
                access=mode.value,
                details={"findings": [finding.to_dict() for finding in findings]},
            ), session_id)

        if level == RiskLevel.L3:
            return self._record(Decision(
                allowed=False,
                risk=level,
                code="risk_l3",
                reason="high-risk operation denied by deterministic policy",
                tool=tool,
                access=mode.value,
            ), session_id)

        if mode == Access.NETWORK:
            grant = self.store.find_capability(capability or "network", "network")
            if grant is None:
                return self._record(Decision(
                    allowed=False,
                    risk=level,
                    code="network_needs_grant",
                    reason="network access requires a one-time grant: "
                           "examprep permissions grant --capability %s" % (capability or "network"),
                    tool=tool,
                    access=mode.value,
                ), session_id)
            return self._record(Decision(
                allowed=True,
                risk=level,
                code="allow_capability",
                reason="network capability granted",
                tool=tool,
                access=mode.value,
                grant_id=grant.id,
            ), session_id)

        effective = level
        grant_id: str | None = None
        paths = raw_paths
        for raw, target in zip(paths, resolved):
            location = self.policy.locate(raw)
            if mode in (Access.WRITE, Access.EXECUTE):
                if location == WORKSPACE:
                    continue
                if location == MATERIALS:
                    return self._record(Decision(
                        allowed=False,
                        risk=level,
                        code="materials_read_only",
                        reason="course materials are read-only: %s" % target,
                        tool=tool,
                        access=mode.value,
                        path=target,
                    ), session_id)
                # Outside the workspace: this is an L2 operation regardless of
                # the tool's declared level.  It is allowed only with a grant.
                effective = RiskLevel.L2
                grant = self.store.find_directory(target, "write")
                if grant is None:
                    return self._record(Decision(
                        allowed=False,
                        risk=level,
                        effective_risk=effective,
                        code="outside_write_needs_grant",
                        reason="path is outside the workspace; grant it first: %s" % self._hint_grant(target),
                        tool=tool,
                        access=mode.value,
                        path=target,
                    ), session_id)
                grant_id = grant.id
            else:  # READ
                if location in (WORKSPACE, MATERIALS):
                    continue
                effective = RiskLevel.L2
                grant = self.store.find_directory(target, "read")
                if grant is None:
                    return self._record(Decision(
                        allowed=False,
                        risk=level,
                        effective_risk=effective,
                        code="outside_read_needs_grant",
                        reason="path is outside the workspace and materials; grant read access first: %s"
                               % self._hint_grant(target),
                        tool=tool,
                        access=mode.value,
                        path=target,
                    ), session_id)
                grant_id = grant.id

        if level == RiskLevel.L2 and not paths:
            name = capability or "system"
            grant = self.store.find_capability(name, "write")
            if grant is None:
                return self._record(Decision(
                    allowed=False,
                    risk=level,
                    code="capability_needs_grant",
                    reason="L2 capability %r requires a one-time grant: "
                           "examprep permissions grant --capability %s" % (name, name),
                    tool=tool,
                    access=mode.value,
                ), session_id)
            grant_id = grant.id

        if count_throttle and mode in (Access.WRITE, Access.EXECUTE) and self.throttle is not None:
            allowed, used = self.throttle.allow()
            if not allowed:
                return self._record(Decision(
                    allowed=False,
                    risk=level,
                    effective_risk=effective,
                    code="write_throttle",
                    reason="write circuit breaker open: %d writes in %.0fs" % (used, self.throttle.window_seconds),
                    tool=tool,
                    access=mode.value,
                    details={"used": used, "limit": self.throttle.limit},
                ), session_id)

        return self._record(Decision(
            allowed=True,
            risk=level,
            effective_risk=effective,
            code="allow",
            reason="allowed",
            tool=tool,
            access=mode.value,
            grant_id=grant_id,
            paths=tuple(resolved),
        ), session_id)

    def check_path(
        self,
        path: str,
        access: str = "write",
        *,
        tool: str = "path",
        risk: RiskLevel | int | str | None = None,
        session_id: str = "",
    ) -> Decision:
        """Check a direct host-side file operation (CLI output, cleanup, …)."""
        mode = Access(access)
        level = RiskLevel.parse(risk if risk is not None else (
            RiskLevel.L0 if mode == Access.READ else RiskLevel.L1
        ))
        return self.check(tool, level, {"path": path}, access=mode, session_id=session_id)

    # -------------------------------------------------------------- grants
    def grant_directory(self, path: str, access: Iterable[str] | str = ("read", "write"), note: str = "") -> Grant:
        """Create/merge a directory grant and audit it."""
        grant = self.store.grant_directory(path, access, note)
        self._audit_grant("permission_grant", grant)
        return grant

    def grant_capability(self, capability: str, access: Iterable[str] | str = ("network",), note: str = "") -> Grant:
        """Create/merge a capability grant and audit it."""
        grant = self.store.grant_capability(capability, access, note)
        self._audit_grant("permission_grant", grant)
        return grant

    def revoke(self, grant_id: str) -> bool:
        """Revoke a grant and audit the outcome."""
        existing = self.store.get(grant_id)
        removed = self.store.revoke(grant_id)
        if self.audit is not None:
            self.audit.log(
                "permission_revoke",
                grant_id=grant_id,
                removed=removed,
                kind=existing.kind if existing else None,
                value=existing.value if existing else None,
            )
        return removed

    def _audit_grant(self, event: str, grant: Grant) -> None:
        if self.audit is not None:
            self.audit.log(event, **grant.to_dict())
