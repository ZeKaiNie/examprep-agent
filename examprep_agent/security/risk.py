# -*- coding: utf-8 -*-
"""Risk levels and permission decisions for the tool layer.

The four levels follow PLAN §5.7:

``L0`` read-only
    Reading material, searching, querying state.  Automatic, audited.
``L1`` workspace write
    Study state, generated notes/cheat sheets, figure crops.  Automatic, but
    the path fence confines it to the workspace.
``L2`` out-of-bounds / network
    Writing outside the workspace, network access, installing packages.  Needs
    a one-time grant stored in the user config directory (never in the
    model-writable workspace); grants can be revoked.
``L3`` high-risk irreversible
    Recursive deletion of a disk root/home, formatting, shutdown, …  Denied
    unconditionally by deterministic code; never a prompt.

The level is a property of the *tool registration*, never of model-supplied
arguments, and a tool can only be elevated by the path policy (an L1 write to a
path outside the workspace is evaluated as L2).  The model cannot grant itself a
level or a permission.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any


class RiskLevel(IntEnum):
    """Tool risk levels (ordered; higher is more dangerous)."""

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3

    @classmethod
    def parse(cls, value: "RiskLevel | int | str") -> "RiskLevel":
        """Accept a member, its integer value, or ``"L0"``…``"L3"``."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            text = value.strip().upper()
            if text in ("L0", "L1", "L2", "L3"):
                return cls(int(text[1]))
            if text.isdigit():
                return cls(int(text))
        if isinstance(value, int) and 0 <= value <= 3:
            return cls(value)
        raise ValueError("invalid risk level: %r" % (value,))

    @property
    def label(self) -> str:
        """Canonical ``L0``…``L3`` label."""
        return "L%d" % int(self)

    @property
    def is_write(self) -> bool:
        """True for levels that may modify state."""
        return self in (RiskLevel.L1, RiskLevel.L2, RiskLevel.L3)


class Access(str, Enum):
    """What a tool wants to do with the paths it receives."""

    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    EXECUTE = "execute"


@dataclass(frozen=True)
class Decision:
    """Outcome of one permission check (also the audit payload)."""

    allowed: bool
    risk: RiskLevel
    code: str
    reason: str
    tool: str = ""
    access: str = ""
    path: str | None = None
    paths: tuple[str, ...] = ()
    grant_id: str | None = None
    effective_risk: RiskLevel | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        """Convenience inverse of :attr:`allowed`."""
        return not self.allowed

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready view."""
        data: dict[str, Any] = {
            "allowed": self.allowed,
            "decision": "allow" if self.allowed else "deny",
            "risk": self.risk.label,
            "effective_risk": (self.effective_risk or self.risk).label,
            "code": self.code,
            "reason": self.reason,
            "tool": self.tool,
            "access": self.access,
        }
        if self.path is not None:
            data["path"] = self.path
        if self.paths:
            data["paths"] = list(self.paths)
        if self.grant_id:
            data["grant_id"] = self.grant_id
        if self.details:
            data["details"] = dict(self.details)
        return data
