# -*- coding: utf-8 -*-
"""Core service layer shared by the CLI, HTTP API and MCP server.

The CLI is a presentation wrapper over :class:`~examprep_agent.service.study.StudyService`;
FastAPI and the MCP server call exactly the same methods, so the teaching,
scheduling, safety and persistence logic exists in one place.
"""
from .costs import aggregate_costs, aggregate_events, resolve_prices
from .errors import (
    EXIT_NO_MATERIAL,
    EXIT_NO_QUESTIONS,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_TO_HTTP,
    ExtrasMissing,
    ServiceError,
    UnknownChapter,
    UnknownQuestion,
    WorkspaceNotFound,
    http_status_for,
)
from .ingest import (
    IngestProgress,
    build_chapters_parallel,
    build_chunks_parallel,
    extract_figures_parallel,
    extract_sources,
    progress_path,
    resolve_workers,
)
from .models import (
    AnswerRequest,
    AnswerResult,
    AskHit,
    AskResult,
    ChapterStatus,
    ChapterSummary,
    CheckResult,
    CheatsheetRequest,
    CheatsheetResult,
    CommandCost,
    CostsResult,
    DoneRequest,
    DoneResult,
    DueReviewView,
    FigureRecord,
    FooterInfo,
    GotoRequest,
    GotoResult,
    GradeRequest,
    GradeResult,
    GraphRequest,
    GraphResult,
    MistakeItem,
    MistakesResult,
    NextResult,
    NoteRequest,
    NoteResult,
    PermissionRequest,
    PermissionsResult,
    PlanResult,
    QuestionRecord,
    QuizRequest,
    QuizResult,
    SessionCost,
    SetupRequest,
    SetupResult,
    StatusResult,
)
from .study import StudyService
from .subagent import (
    SubAgentLimits,
    SubAgentResult,
    grading_subagent,
    retrieval_subagent,
    run_subagent,
    sanitize_summary,
)

__all__ = [
    "AnswerRequest", "AnswerResult", "AskHit", "AskResult", "ChapterStatus",
    "ChapterSummary", "CheckResult", "CheatsheetRequest", "CheatsheetResult",
    "CommandCost", "CostsResult", "DoneRequest", "DoneResult", "DueReviewView",
    "EXIT_NO_MATERIAL", "EXIT_NO_QUESTIONS", "EXIT_OK", "EXIT_TO_HTTP", "EXIT_USAGE",
    "ExtrasMissing", "FigureRecord", "FooterInfo", "GotoRequest", "GotoResult",
    "GradeRequest", "GradeResult", "GraphRequest", "GraphResult", "IngestProgress",
    "MistakeItem", "MistakesResult", "NextResult", "NoteRequest", "NoteResult",
    "PermissionRequest", "PermissionsResult", "PlanResult", "QuestionRecord",
    "QuizRequest", "QuizResult", "ServiceError", "SessionCost", "SetupRequest",
    "SetupResult", "StatusResult", "StudyService", "SubAgentLimits", "SubAgentResult",
    "UnknownChapter", "UnknownQuestion", "WorkspaceNotFound", "aggregate_costs",
    "aggregate_events", "build_chapters_parallel", "build_chunks_parallel",
    "extract_figures_parallel", "extract_sources", "grading_subagent",
    "http_status_for", "progress_path", "resolve_prices", "resolve_workers",
    "retrieval_subagent", "run_subagent", "sanitize_summary",
]
