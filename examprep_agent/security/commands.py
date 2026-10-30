# -*- coding: utf-8 -*-
"""Deterministic scanner for destructive commands and credential exfiltration.

The project intentionally exposes no general shell tool.  This scanner is still
required by PLAN §5.7 because (a) a future exec-type tool must be guarded before
it ever runs and (b) course material may contain command-looking text that a
model could be tricked into executing.  It is a static, case/width/whitespace
insensitive matcher — no shell, no evaluation.

Findings are *recorded* (audit + state) and any exec-type tool call that carries
one is denied unconditionally by the permission gate.  Materials are never
modified; they are displayed inside the usual data fence.
"""
from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .audit import redact_text

_ZERO_WIDTH = re.compile("[­᠎​-‏‪-‮⁠-⁤﻿]")
_WS = re.compile(r"[ \t\r\f\v]+")
# Shell line continuation: ``rm -rf \`` + newline + ``/``.
_CONTINUATION = re.compile(r"\\\s*\r?\n\s*")
# Static home-directory aliases, so quoting/expansion cannot hide the target.
_HOME_ALIAS = re.compile(
    r"\$\{\s*home\s*\}|\$home\b|\$\{\s*userprofile\s*\}|\$env:userprofile\b|"
    r"\$env:home\b|%userprofile%",
    re.I,
)
# Output redirections are not part of the destructive target.
_REDIRECT = re.compile(
    r"\d?>>?\s*&\d+|\d?>>?\s*[^\s;&|]+|\d?>&\d+"
)
_SHELLS = r"(?:sh|bash|zsh|dash|ksh|pwsh|powershell|c?md|python[0-9.]*)"
_B64_PIPE = re.compile(
    r"(?:echo|printf|write-output)\s+([A-Za-z0-9+/=]{8,})\s*\|\s*"
    r"(?:base64|frombase64string|\[convert\]::frombase64string)\b[^\n]*\|\s*" + _SHELLS,
    re.I,
)
_B64_HERESTRING = re.compile(
    r"(?:base64\s+(?:-d|--decode|--decode-string)|\[convert\]::frombase64string)\s*"
    r"(?:<<<|\(\s*)?\s*['\"]?([A-Za-z0-9+/=]{8,})",
    re.I,
)
_ENCODED_COMMAND = re.compile(r"-encodedcommand\s+['\"]?([A-Za-z0-9+/=]{8,})", re.I)


class DangerousCommandError(PermissionError):
    """Raised when a command-like string matches a hard-blocked pattern."""

    def __init__(self, findings: Iterable["SecurityFinding"]) -> None:
        self.findings = list(findings)
        super().__init__("dangerous command blocked: %s" % ", ".join(f.pattern for f in self.findings))


@dataclass(frozen=True)
class SecurityFinding:
    """One matched dangerous/exfiltration pattern."""

    kind: str
    pattern: str
    excerpt: str
    line: int = 0
    source: str = ""

    def to_dict(self) -> dict[str, object]:
        """JSON-ready view (excerpt is redacted before storage)."""
        return {
            "kind": self.kind,
            "pattern": self.pattern,
            "excerpt": self.excerpt,
            "line": self.line,
            "source": self.source,
        }


def _norm(text: str) -> str:
    """Shell-ish normalisation used for *detection only* (never evaluation).

    It joins backslash continuations, removes paired/loose quotes, expands the
    obvious static home aliases (``$HOME``, ``${HOME}``, ``%USERPROFILE%``,
    ``$env:USERPROFILE``) and drops output redirections.  That makes the
    deterministic patterns see the same target a shell would, without ever
    running anything.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTINUATION.sub(" ", text)
    text = text.replace('"', "").replace("'", "").replace("`", "")
    text = _HOME_ALIAS.sub("~", text)
    text = _REDIRECT.sub(" ", text)
    return _WS.sub(" ", text.lower())


def _norm_encoded(text: str) -> str:
    """Quote-free copy that preserves base64 case (lowercasing corrupts it)."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTINUATION.sub(" ", text)
    return text.replace('"', "").replace("'", "").replace("`", "")


def _excerpt(line: str, limit: int = 160) -> str:
    clean = "".join(ch if ch.isprintable() else " " for ch in line)
    clean = _WS.sub(" ", clean).strip()
    clean = redact_text(clean)
    return clean[:limit] + ("…" if len(clean) > limit else "")


# (kind, pattern-name, regex) — regexes run on the normalised text.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # ------------------------------------------------ recursive deletion
    # These names can appear mid-sentence in poisoned material ("then run
    # rm -rf /"), so they are matched anywhere, not only at a command start.
    ("recursive_delete_root", "rm_root_home", re.compile(
        # Option order/forms: -rf, -r -f, --recursive --force, and the bare
        # ``--`` separator are all accepted.  Quotes/aliases/redirects were
        # already normalised away.
        r"\brm\s+(?:--[a-z-]+\s+|-[a-z]+\s+|--\s+){0,8}(?:--no-preserve-root\s+)?"
        r"(?:/|~|\$home|/home(?:/[\w.-]+)*/?|/users(?:/[\w.-]+)*/?|"
        r"/(?:etc|usr|bin|sbin|boot|var|opt|root|dev|proc|sys)(?:/[\w.-]+)*/?|"
        r"[a-z]:[\\/](?:users(?:[\\/][\w.-]+)*)?)"
        r"(?:/?\*)?\s*(?:$|[;&|#])"
    )),
    ("recursive_delete_wildcard", "rm_wildcard", re.compile(
        r"\brm\s+(?:--[a-z-]+\s+|-[a-z]+\s+|--\s+){0,8}"
        r"(?:\*|/\*|~/\*|\.?/\*|[\w.-]+/\*)\s*(?:$|[;&|#])"
    )),
    ("recursive_delete_root", "windows_del_root", re.compile(
        # Drive root, a user/system directory tree (del /s /q C:\Users\x), or
        # the profile alias (normalised to ``~``).
        r"\b(?:del|erase)\s+(?:/[a-z]+\s+)*"
        r"(?:[a-z]:[\\/](?:(?:users|windows|program files)(?:[\\/][\w.*-]+)*)?|~)"
        r"(?:\*|\s|$|[\\/])"
    )),
    ("recursive_delete_root", "windows_rd_root", re.compile(
        r"\b(?:rd|rmdir)\s+/s(?:\s+/q)?\s+[a-z]:"
    )),
    ("recursive_delete_root", "powershell_remove_item", re.compile(
        r"remove-item\b[^\n]*-recurse[^\n]*(?:[a-z]:[\\/]|/|~)"
    )),
    # ------------------------------------------------ disk formatting
    ("format_disk", "format_drive", re.compile(r"\bformat\s+[a-z]:")),
    ("format_disk", "mkfs", re.compile(r"\bmkfs(?:\.\w+)?\s+")),
    ("format_disk", "disk_write", re.compile(r"\bdd\s+[^\n]*\bof=/dev/(?:sd|nvme|hd|vd|mmcblk|disk)")),
    ("format_disk", "diskpart", re.compile(r"\b(?:diskpart|format-volume|clear-disk|initialize-disk)\b")),
    ("format_disk", "bcdedit", re.compile(r"\bbcdedit\b")),
    # ------------------------------------------------ shutdown / reboot
    ("shutdown", "shutdown_command", re.compile(
        # Accept Windows style ``shutdown /s /t 0`` as well as ``-h now``;
        # prose ("the reboot of the server") still fails because the next word
        # is not an option/number and the sentence does not end there.
        r"\b(?:shutdown|reboot|poweroff|halt)\b"
        r"(?:\s+(?:-[a-z]+|/[a-z]+(?:\s+\d+)?|\+\d+|now))*"
        r"\s*(?:$|[.;!?，。；！？`])"
    )),
    ("shutdown", "init_zero", re.compile(r"\binit\s+0\b")),
    ("shutdown", "systemctl_power", re.compile(r"\bsystemctl\s+(?:poweroff|reboot|halt)\b")),
    # ------------------------------------------------ destructive permissions
    ("permission_destruction", "chmod_root", re.compile(r"\bchmod\s+-r\s+0{3,4}\s+/")),
    ("permission_destruction", "chown_root", re.compile(r"\bchown\s+-r\s+[^\n]*\s+/(?:\s|$)")),
    ("permission_destruction", "reg_delete", re.compile(r"\breg\s+delete\s+hklm\b")),
    # ------------------------------------------------ fork bomb
    ("fork_bomb", "bash_fork_bomb", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    # ------------------------------------------------ credential theft
    ("credential_access", "private_key_read", re.compile(
        r"\b(?:cat|type|get-content|more|less)\s+[^\n]*(?:id_rsa|id_ed25519|\.ssh/|\.env\b|credentials\b)"
    )),
    ("secret_exfiltration", "secret_to_url", re.compile(
        r"(?:api[-_ ]?key|secret|token|password|passwd|密钥|令牌|凭证)[^\n]{0,80}"
        r"(?:send|upload|post|forward|leak|exfiltrate|email|发送|上传|转发|泄露|发给)[^\n]{0,80}https?://"
    )),
    ("secret_exfiltration", "send_secret_to_url", re.compile(
        r"(?:send|upload|post|forward|leak|exfiltrate|email|发送|上传|转发|泄露|发给)[^\n]{0,80}"
        r"(?:api[-_ ]?key|secret|token|password|passwd|credentials?|密钥|令牌|凭证)[^\n]{0,80}https?://"
    )),
    ("secret_exfiltration", "url_plus_secret", re.compile(
        r"https?://[^\s]+[^\n]{0,60}(?:api[-_ ]?key|secret|token|password|密钥|令牌)"
    )),
    ("secret_exfiltration", "curl_with_secret", re.compile(
        r"\b(?:curl|wget)\b[^\n]*(?:-d|--data|--post-data)[^\n]*"
        r"(?:api[-_ ]?key|token|password|secret|密钥|令牌)"
    )),
    ("secret_exfiltration", "env_to_network", re.compile(
        r"(?:cat|type|get-content)\s+[^\n]*\.env[^\n]*(?:\||>|curl|wget|https?://)"
    )),
)

# Course material is multi-line; `^`/`$` must anchor on every line, otherwise a
# command inside a code fence on line 40 is never seen.
_PATTERNS = tuple(
    (kind, name, re.compile(pattern.pattern, pattern.flags | re.M))
    for kind, name, pattern in _PATTERNS
)


class DangerousCommandScanner:
    """Static matcher for command-like text and credential-exfiltration prose."""

    def __init__(self, patterns: tuple[tuple[str, str, re.Pattern[str]], ...] = _PATTERNS) -> None:
        self.patterns = patterns

    @staticmethod
    def _decode(text: str) -> str | None:
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
        except (ValueError, TypeError):
            return None
        for encoding in ("utf-8", "utf-16-le"):
            decoded = raw.decode(encoding, errors="replace").replace("\x00", "")
            if not decoded:
                continue
            printable = sum(1 for char in decoded if char.isprintable() or char in "\n\t")
            if printable >= 0.7 * len(decoded):
                return decoded
        return None

    def _encoded_findings(self, normalised: str, source: str, depth: int = 0) -> list[SecurityFinding]:
        """Decode obvious base64 payloads and scan what they actually contain."""
        payloads: list[str] = []
        for regex in (_B64_PIPE, _B64_HERESTRING, _ENCODED_COMMAND):
            for match in regex.finditer(normalised):
                encoded = match.group(1)
                if len(encoded) > 65_536:  # refuse to expand unbounded material
                    continue
                decoded = self._decode(encoded)
                if decoded:
                    payloads.append(decoded)
        findings: list[SecurityFinding] = []
        for decoded in payloads:
            nested = DangerousCommandScanner(self.patterns).scan(
                decoded, source="encoded:%s" % source, _depth=depth + 1)
            if not nested:
                continue
            findings.append(SecurityFinding(
                kind="encoded_command",
                pattern="base64_payload",
                excerpt=_excerpt(decoded),
                line=1,
                source=source,
            ))
        return findings

    def scan(self, text: str, source: str = "", _depth: int = 0) -> list[SecurityFinding]:
        """Return every finding in ``text`` (deduplicated, stable order)."""
        if not text:
            return []
        normalised = _norm(text)
        original_lines = text.splitlines() or [text]
        findings: list[SecurityFinding] = []
        seen: set[tuple[str, str, int]] = set()
        for kind, name, pattern in self.patterns:
            for match in pattern.finditer(normalised):
                line_no = normalised.count("\n", 0, match.start()) + 1
                key = (kind, name, line_no)
                if key in seen:
                    continue
                seen.add(key)
                original = original_lines[line_no - 1] if line_no - 1 < len(original_lines) else match.group(0)
                findings.append(SecurityFinding(
                    kind=kind,
                    pattern=name,
                    excerpt=_excerpt(original),
                    line=line_no,
                    source=source,
                ))
        if _depth < 2:
            findings.extend(self._encoded_findings(_norm_encoded(text), source, _depth))
        findings.sort(key=lambda item: (item.line, item.kind, item.pattern))
        return findings

    def is_dangerous(self, text: str) -> bool:
        """Convenience boolean form."""
        return bool(self.scan(text))

    def assert_safe(self, text: str, source: str = "") -> None:
        """Raise :class:`DangerousCommandError` when anything matches."""
        findings = self.scan(text, source=source)
        if findings:
            raise DangerousCommandError(findings)


def scan_text(text: str, source: str = "") -> list[SecurityFinding]:
    """Module-level convenience wrapper."""
    return DangerousCommandScanner().scan(text, source=source)
