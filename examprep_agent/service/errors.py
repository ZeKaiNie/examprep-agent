# -*- coding: utf-8 -*-
"""Service-layer errors and the CLI-exit-code ↔ HTTP-status mapping.

Both the CLI and the HTTP API speak the same vocabulary: a service method
either returns a structured result (with ``exit_code`` 0/2/3/4) or raises
:class:`ServiceError` for an invalid request/workspace.  The CLI returns the
exit code verbatim; FastAPI maps it with :func:`http_status_for`, so a caller
cannot observe two different meanings for "no such workspace" depending on the
transport.
"""
from __future__ import annotations

from typing import Any

#: CLI exit codes used by the deterministic commands (unchanged since P0).
EXIT_OK = 0
EXIT_USAGE = 2          # missing workspace / unknown chapter/question / bad args
EXIT_NO_QUESTIONS = 3   # quiz selected nothing
EXIT_NO_MATERIAL = 4    # ask found no material match

#: HTTP status for each CLI exit code (semantics, not the number itself).
EXIT_TO_HTTP: dict[int, int] = {
    EXIT_OK: 200,
    EXIT_USAGE: 400,
    EXIT_NO_QUESTIONS: 404,
    EXIT_NO_MATERIAL: 404,
}


def http_status_for(exit_code: int) -> int:
    """HTTP status carrying the same meaning as a CLI exit code."""
    return EXIT_TO_HTTP.get(int(exit_code), 500)


class ServiceError(Exception):
    """A structured, transport-neutral service failure.

    Attributes:
        message: human-readable explanation (never contains secrets).
        code: stable machine code (``workspace_not_found``, ``unknown_question``…).
        exit_code: the CLI exit code this failure maps to.
        details: optional JSON-safe context.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "service_error",
        exit_code: int = EXIT_USAGE,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = int(exit_code)
        self.details = dict(details or {})

    @property
    def http_status(self) -> int:
        """HTTP status that carries the CLI semantics."""
        return http_status_for(self.exit_code)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready error payload (shared by HTTP and MCP)."""
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.message,
            "code": self.code,
            "exit_code": self.exit_code,
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class WorkspaceNotFound(ServiceError):
    """No study workspace exists at the requested/environment/pointer path."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message or "no study workspace found; run: examprep setup <materials folder>",
            code="workspace_not_found",
            exit_code=EXIT_USAGE,
        )


class UnknownQuestion(ServiceError):
    """A question id was not found in the workspace bank."""

    def __init__(self, qid: str, workspace: str | None = None) -> None:
        details: dict[str, Any] = {"qid": qid}
        if workspace:
            details["workspace"] = workspace
        super().__init__("unknown question id: %s" % qid, code="unknown_question",
                         exit_code=EXIT_USAGE, details=details)


class UnknownChapter(ServiceError):
    """A chapter number was not found in the study state."""

    def __init__(self, chapter: int, workspace: str | None = None) -> None:
        details: dict[str, Any] = {"chapter": chapter}
        if workspace:
            details["workspace"] = workspace
        super().__init__("no such chapter: %s" % chapter, code="unknown_chapter",
                         exit_code=EXIT_USAGE, details=details)


class ExtrasMissing(RuntimeError):
    """An optional integration was imported without its extras installed."""

    def __init__(self, extra: str, hint: str | None = None) -> None:
        self.extra = extra
        self.hint = hint or "pip install 'examprep-agent[%s]'" % extra
        super().__init__(
            "optional dependency for %r is not installed: %s" % (extra, self.hint)
        )
