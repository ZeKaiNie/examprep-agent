# -*- coding: utf-8 -*-
"""FastAPI transport over :class:`~examprep_agent.service.study.StudyService`.

Optional extra: ``pip install 'examprep-agent[serve]'``.  Importing this module
without FastAPI is safe; :func:`create_app` raises
:class:`~examprep_agent.service.errors.ExtrasMissing` with the install hint.

Sessions are isolated by workspace: every request resolves its workspace and
loads the durable ``Session`` from disk for that call.  There is no process-wide
mutable study session, so two workspaces (or two users pointing at different
directories) cannot see each other's state — the same model as the CLI.

Error semantics match the CLI exactly: the service result's ``exit_code`` is
mapped to HTTP by :func:`~examprep_agent.service.errors.http_status_for`
(2 → 400, 3/4 → 404) and the JSON body still carries ``exit_code``.
"""
from __future__ import annotations

from typing import Any

from .. import __version__
from ..service import (
    AnswerRequest,
    CheatsheetRequest,
    DoneRequest,
    GradeRequest,
    GraphRequest,
    GotoRequest,
    NoteRequest,
    PermissionRequest,
    QuizRequest,
    ServiceError,
    SetupRequest,
    StudyService,
    http_status_for,
)
from ..service.errors import ExtrasMissing
from . import mcp_available

try:  # optional dependency: never fail the import
    from fastapi import FastAPI, Query
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in a no-extras environment
    FastAPI = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    RequestValidationError = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    FASTAPI_AVAILABLE = False


def _require_fastapi() -> None:
    if not FASTAPI_AVAILABLE:
        raise ExtrasMissing("serve", "pip install 'examprep-agent[serve]'")


def create_app(
    service: StudyService | None = None,
    *,
    pointer: str | None = None,
    cwd: str | None = None,
) -> Any:
    """Build the FastAPI app (raises :class:`ExtrasMissing` without the extra)."""
    _require_fastapi()
    app = FastAPI(
        title="examprep-agent",
        version=__version__,
        description="Long-term/cram study agent service layer (workspace-isolated).",
    )
    svc = service or StudyService(pointer=pointer, cwd=cwd)

    def respond(result: Any) -> Any:
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code:
            return JSONResponse(status_code=http_status_for(exit_code),
                                content=result.model_dump())
        return result

    def fail(exc: ServiceError) -> Any:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    # S2: Pydantic request-validation failures must use the same error model
    # and CLI exit-code semantics as every other bad request (400/exit 2),
    # instead of FastAPI's default 422 {"detail": [...]}.
    @app.exception_handler(RequestValidationError)
    def validation_error(request: Any, exc: Any) -> Any:
        pieces: list[str] = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error.get("loc", ()))
            pieces.append(("%s: %s" % (location, error.get("msg", "invalid")))
                          if location else str(error.get("msg", "invalid")))
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "; ".join(pieces) or "invalid request",
            "code": "validation_error",
            "exit_code": 2,
        })

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "service": "examprep-agent",
            "extras": {"serve": True, "mcp": mcp_available()},
        }

    @app.post("/setup")
    def setup(request: SetupRequest) -> Any:
        try:
            return respond(svc.setup(request, audit_tool="http.setup"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/status")
    def status(workspace: str | None = Query(default=None)) -> Any:
        try:
            return respond(svc.status(workspace, audit_tool="http.status"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/plan")
    def plan(
        workspace: str | None = Query(default=None),
        days: int | None = Query(default=None),
        date: str | None = Query(default=None),
    ) -> Any:
        try:
            return respond(svc.plan(workspace, days=days, exam_date=date,
                                    audit_tool="http.plan"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/next")
    def next_action(
        workspace: str | None = Query(default=None),
        chars: int | None = Query(default=None),
        repeat: bool = Query(default=False),
        back: bool = Query(default=False),
    ) -> Any:
        try:
            return respond(svc.next(workspace, chars=chars, repeat=repeat, back=back,
                                    audit_tool="http.next"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/ask")
    def ask(
        q: str = Query(..., min_length=1),
        workspace: str | None = Query(default=None),
        k: int = Query(default=5, ge=1, le=50),
        chapter: int | None = Query(default=None),
        chars: int = Query(default=700, ge=1),
    ) -> Any:
        try:
            return respond(svc.ask(workspace, q, k=k, chapter=chapter, chars=chars,
                                   audit_tool="http.ask"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/quiz")
    def quiz(request: QuizRequest) -> Any:
        try:
            return respond(svc.quiz(request, audit_tool="http.quiz"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/check/{qid}")
    def check(qid: str, workspace: str | None = Query(default=None)) -> Any:
        try:
            return respond(svc.check(workspace, qid, audit_tool="http.check"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/answer")
    def answer(request: AnswerRequest) -> Any:
        try:
            return respond(svc.answer(request, audit_tool="http.answer"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/grade")
    def grade(request: GradeRequest) -> Any:
        try:
            return respond(svc.grade(request, audit_tool="http.grade"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/done")
    def done(request: DoneRequest) -> Any:
        try:
            return respond(svc.done(request, audit_tool="http.done"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/note")
    def note(request: NoteRequest) -> Any:
        try:
            return respond(svc.note(request, audit_tool="http.note"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/mistakes")
    def mistakes(workspace: str | None = Query(default=None),
                 chapter: int | None = Query(default=None)) -> Any:
        try:
            return respond(svc.mistakes(workspace, chapter, audit_tool="http.mistakes"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/cheatsheet")
    def cheatsheet(request: CheatsheetRequest) -> Any:
        try:
            return respond(svc.cheatsheet(request, audit_tool="http.cheatsheet"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/goto")
    def goto(request: GotoRequest) -> Any:
        try:
            return respond(svc.goto(request, audit_tool="http.goto"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/graph")
    def graph(request: GraphRequest) -> Any:
        try:
            return respond(svc.graph(request, audit_tool="http.graph"))
        except ServiceError as exc:
            return fail(exc)

    @app.post("/permissions")
    def permissions(request: PermissionRequest) -> Any:
        try:
            return respond(svc.permissions(request, audit_tool="http.permissions"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/permissions")
    def permissions_list(workspace: str | None = Query(default=None)) -> Any:
        try:
            return respond(svc.permissions(PermissionRequest(workspace=workspace, action="list"),
                                          audit_tool="http.permissions"))
        except ServiceError as exc:
            return fail(exc)

    @app.get("/costs")
    def costs(
        workspace: str | None = Query(default=None),
        input_price: float | None = Query(default=None, ge=0),
        output_price: float | None = Query(default=None, ge=0),
        command: str | None = Query(default=None),
    ) -> Any:
        try:
            return respond(svc.costs(workspace, input_price=input_price,
                                     output_price=output_price, command=command))
        except ServiceError as exc:
            return fail(exc)

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
    """Run the HTTP service with uvicorn (``serve`` extra installs it)."""
    _require_fastapi()
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - extra partially installed
        raise ExtrasMissing("serve", "pip install 'examprep-agent[serve]'") from exc
    uvicorn.run(create_app(**kwargs), host=host, port=port)


def main(argv: Any = None) -> int:
    """Console/``python -m`` entry point for the FastAPI service."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="examprep-serve",
        description="Serve the examprep-agent REST API (workspace-scoped).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="TCP port (default 8000)")
    parser.add_argument("--pointer", default=None, help="workspace pointer file to use")
    parser.add_argument("--cwd", default=None, help="directory used for local workspace discovery")
    args = parser.parse_args(argv)
    try:
        serve(args.host, args.port, pointer=args.pointer, cwd=args.cwd)
    except ExtrasMissing as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())


__all__ = ["FASTAPI_AVAILABLE", "create_app", "main", "serve"]
