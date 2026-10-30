# -*- coding: utf-8 -*-
"""Append-only JSONL audit log with secret redaction.

Every permission decision (allow/deny), every grant/revocation and every
material scan result is written as one JSON object per line under
``<workspace>/logs/audit-YYYY-MM-DD.jsonl``.  Values are redacted *before*
serialisation so a leaked API key never reaches disk, not even in an error
message or a tool argument.

The redactor is deliberately conservative: it targets well-known key shapes
(``sk-…``, ``ghp_…``, ``AKIA…``, ``AIza…``, bearer tokens, private-key blocks,
``api_key=…`` assignments) and secret-named fields.  :func:`contains_secret`
answers whether a value contains one of those strong shapes; the permission
gate uses it to refuse tools that try to move a credential.
"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

REDACTED = "[REDACTED]"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Private key blocks (multiline).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED_PRIVATE_KEY]"),
    # OpenAI / DeepSeek / Anthropic style keys.
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{8,}"), "[REDACTED_API_KEY]"),
    # GitHub tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "[REDACTED_GITHUB_TOKEN]"),
    # AWS access key ids.
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Google API keys.
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    # Bearer / Bot tokens in headers or text.
    (re.compile(r"(?i)\b(Bearer|Bot)\s+[A-Za-z0-9\-._~+/]{10,}=*"), r"\1 " + REDACTED),
    # key=value / key: value assignments for secret-named fields.
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
                r"secret[_-]?key|password|passwd|token)\b\s*[:=]\s*['\"]?[^\s'\"，,;]{6,}"),
     r"\1=" + REDACTED),
)

# Strong shapes only: used to block tools that try to move a credential.
_STRONG_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)\b(?:Bearer|Bot)\s+[A-Za-z0-9\-._~+/]{20,}=*"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|secret[_-]?key|"
               r"password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{12,}"),
)

_SECRET_FIELD_NAMES = frozenset(
    name.lower()
    for name in (
        "api_key", "apikey", "access_token", "auth_token", "client_secret",
        "secret_key", "password", "passwd", "token", "authorization",
        "deepseek_api_key", "openai_api_key",
    )
)


def redact_text(text: str) -> str:
    """Return ``text`` with recognised credentials replaced by placeholders."""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_secrets(value: Any) -> Any:
    """Recursively redact credentials in strings, dict values and sequences.

    Dict keys are kept (so audit structure survives); a value under a
    secret-named key is replaced wholesale.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_FIELD_NAMES:
                out[key] = REDACTED
            else:
                out[key] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, set):
        return {redact_secrets(item) for item in value}
    return value


def contains_secret(value: Any) -> bool:
    """True when a value contains something that looks like a live credential."""
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _STRONG_SECRET_PATTERNS)
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SECRET_FIELD_NAMES and isinstance(item, str) and item.strip():
                return True
            if contains_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple, set)):
        return any(contains_secret(item) for item in value)
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def audit_path(workspace: str, today: str | None = None) -> str:
    """Daily audit log path for a workspace (kept apart from agent logs)."""
    day = today or datetime.now(timezone.utc).date().isoformat()
    return os.path.join(workspace, "audit", "audit-%s.jsonl" % day)


class AuditLog:
    """Append-only, redacting JSONL audit log.

    The log is intentionally simple and crash-tolerant: one ``write`` + ``flush``
    per event, no rewriting.  A reader that meets a truncated final line simply
    skips it (see :func:`read_jsonl`).
    """

    def __init__(self, workspace: str, path: str | None = None) -> None:
        self.workspace = os.path.abspath(workspace)
        self.path = path or audit_path(workspace)

    def log(self, event: str, **fields: Any) -> dict[str, Any]:
        """Write one redacted event and return the stored object."""
        entry: dict[str, Any] = {"ts": _utc_now(), "event": event}
        entry.update(copy.deepcopy(redact_secrets(fields)))
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # pragma: no cover - filesystem dependent
                pass
        return entry

    def decision(self, decision: Any, *, session_id: str = "", source: str = "tool") -> dict[str, Any]:
        """Record a :class:`~examprep_agent.security.risk.Decision`."""
        payload = decision.to_dict() if hasattr(decision, "to_dict") else dict(decision)
        return self.log("permission_decision", session_id=session_id, source=source, **payload)

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the last ``limit`` well-formed events (oldest first)."""
        return read_jsonl(self.path, limit=limit)


def read_jsonl(path: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Read JSONL defensively: malformed or truncated lines are skipped."""
    if not os.path.exists(path):
        return []
    if limit == 0:
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict):
                    events.append(item)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        return events[-limit:]
    return events


def iter_jsonl(path: str) -> Iterable[dict[str, Any]]:
    """Yield well-formed events lazily (malformed lines skipped)."""
    yield from read_jsonl(path)
