# -*- coding: utf-8 -*-
"""Structured service request/response models.

The CLI renders these models, FastAPI serialises them and the MCP server
returns their JSON form.  Business logic never lives here — only the shared
shape of inputs and outputs.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ------------------------------------------------------------------ shared
class ReadError(_Model):
    file: str
    error: str


class ChapterSummary(_Model):
    n: int
    title: str
    sources: list[str] = Field(default_factory=list)
    parts: int = 0
    questions: int = 0
    exam_questions: int = 0


class QuestionRecord(_Model):
    """A quiz-bank item as stored in ``quiz_bank.json``."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    chapter: int | None = None
    question: str = ""
    options: list[str] = Field(default_factory=list)
    answer: str | None = None
    type: str = "subjective"
    points: int | None = None
    source: dict[str, Any] = Field(default_factory=dict)
    answer_source: dict[str, Any] | None = None
    figures: list[str] = Field(default_factory=list)
    answer_figures: list[str] = Field(default_factory=list)
    chapter_guessed: bool = False


class FigureRecord(_Model):
    file: str = ""
    page: int = 0
    path: str = ""
    kind: str = "figure"
    qid: str | None = None
    box: list[float] | None = None


class FooterInfo(_Model):
    """Everything the deterministic ``📍`` guidance line needs."""

    current_chapter: int | None = None
    current_title: str = ""
    current_part: int = 0
    current_parts: int = 0
    chapter_right: int = 0
    chapter_asked: int = 0
    mastery: float = 0.0
    done: int = 0
    total: int = 0
    open_mistakes: int = 0
    days_left: int | None = None


class DueReviewView(_Model):
    qid: str
    kp: str | None = None
    chapter: int | None = None
    due: str = ""
    retrievability: float = 0.0
    question: str = ""
    source: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ setup
class SetupRequest(_Model):
    materials: str
    workspace: str | None = None
    lang: Literal["zh", "en"] | None = None
    days: int | None = None
    date: str | None = None
    mode: Literal["auto", "longterm", "cram"] = "auto"
    profile: Literal["small", "standard", "large"] | None = None
    name: str | None = None
    start: int | None = None
    slice: int | None = None
    fresh: bool = False
    workers: int | None = Field(
        default=None, ge=1, le=64,
        description="ingest worker threads; 1 forces the serial pipeline",
    )


class SetupResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    course: str
    language: str
    mode: str
    mode_reason_code: str
    mode_reason_params: dict[str, Any] = Field(default_factory=dict)
    exam_date: str | None = None
    days_left: int | None = None
    files: dict[str, int] = Field(default_factory=dict)          # kind → count
    chapters: list[ChapterSummary] = Field(default_factory=list)
    questions: int = 0
    answered: int = 0
    figures: int = 0
    question_figures: int = 0
    scanned_pages: int = 0
    has_pdf_sources: bool = False
    pdfium_available: bool = True
    pdf_missing: list[str] = Field(default_factory=list)
    read_errors: list[ReadError] = Field(default_factory=list)
    no_text_files: list[str] = Field(default_factory=list)
    figure_failures: list[str] = Field(default_factory=list)
    injection_files: dict[str, list[int]] = Field(default_factory=dict)
    security_findings: int = 0
    elapsed_seconds: float = 0.0
    footer: FooterInfo = Field(default_factory=FooterInfo)


# ------------------------------------------------------------------ status
class ChapterStatus(_Model):
    n: int
    title: str
    status: str
    part: int = 0
    parts: int = 0
    right: int = 0
    asked: int = 0
    mastery: float = 0.0
    current: bool = False


class StatusResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    course: str
    language: str
    mode: str
    exam_date: str | None = None
    days_left: int | None = None
    done: int = 0
    total: int = 0
    chapters: list[ChapterStatus] = Field(default_factory=list)
    open_mistakes: int = 0
    confusions: int = 0
    graph: dict[str, Any] = Field(default_factory=dict)
    due_count: int = 0
    retention: float | None = None
    daily_new_used: int = 0
    daily_new_limit: int = 0
    today_chapters: list[int] = Field(default_factory=list)
    footer: FooterInfo = Field(default_factory=FooterInfo)


# ------------------------------------------------------------------ plan
class PlanDay(_Model):
    day: int
    chapters: list[ChapterSummary] = Field(default_factory=list)


class PlanResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    exam_date: str | None = None
    days_left: int | None = None
    mode: str = "longterm"
    done: int = 0
    total: int = 0
    has_exam_date: bool = False
    due: list[DueReviewView] = Field(default_factory=list)
    days: list[PlanDay] = Field(default_factory=list)
    focus: list[int] = Field(default_factory=list)
    today_review_count: int = 0
    today_chapters: list[int] = Field(default_factory=list)
    all_done: bool = False
    footer: FooterInfo = Field(default_factory=FooterInfo)


# ------------------------------------------------------------------ next
class NextResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    kind: Literal[
        "due_review", "prereq_blocked", "daily_limit", "all_done",
        "no_text", "chapter_end", "material",
    ]
    chapter: int | None = None
    chapter_title: str = ""
    part: int = 0
    parts: int = 0
    materials: str = ""
    chapter_sources: list[str] = Field(default_factory=list)
    due: list[DueReviewView] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    part_text: str = ""
    anchors: list[str] = Field(default_factory=list)
    figures: list[FigureRecord] = Field(default_factory=list)
    chapter_figures: list[str] = Field(default_factory=list)
    examples: list[QuestionRecord] = Field(default_factory=list)
    examples_total: int = 0
    next_command: str = ""
    footer: FooterInfo = Field(default_factory=FooterInfo)


# ------------------------------------------------------------------ ask / quiz
class AskHit(_Model):
    chapter: int | None = None
    file: str = ""
    page: int = 0
    score: float = 0.0
    text: str = ""
    figures: list[str] = Field(default_factory=list)


class AskResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    query: str = ""
    hits: list[AskHit] = Field(default_factory=list)
    found: bool = False
    subagent_summary: str | None = None
    footer: FooterInfo = Field(default_factory=FooterInfo)


class QuizRequest(_Model):
    workspace: str | None = None
    chapter: int | None = None
    # S3: a negative n used to fall through to Python slicing (``pool[:-1]``);
    # it is now a validation error at every transport.
    n: int | None = Field(default=None, ge=1, le=200)
    all: bool = False
    review: bool = False


class QuizResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    mode: Literal["review", "all", "chapter"] = "chapter"
    chapter: int | None = None
    questions: list[QuestionRecord] = Field(default_factory=list)
    found: bool = False
    footer: FooterInfo = Field(default_factory=FooterInfo)


class CheckResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    qid: str = ""
    question: QuestionRecord | None = None
    footer: FooterInfo = Field(default_factory=FooterInfo)


# ------------------------------------------------------------------ answer / grade
class AnswerRequest(_Model):
    workspace: str | None = None
    qid: str
    result: Literal["right", "wrong", "skip"]
    note: str | None = None


class AnswerResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    qid: str
    result: str
    chapter: int | None = None
    chapter_right: int = 0
    chapter_asked: int = 0
    open_mistakes: int = 0
    mastery: float = 0.0
    info: dict[str, Any] = Field(default_factory=dict)
    next_command: str = ""
    footer: FooterInfo = Field(default_factory=FooterInfo)


class GradeRequest(_Model):
    workspace: str | None = None
    qid: str
    reply: str
    subagent: bool = False


class GradeResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    qid: str
    verdict: str = "unsure"       # right | wrong | unsure
    key: str | None = None
    recorded: bool = False
    question: QuestionRecord | None = None
    info: dict[str, Any] = Field(default_factory=dict)
    chapter: int | None = None
    chapter_right: int = 0
    chapter_asked: int = 0
    open_mistakes: int = 0
    mastery: float = 0.0
    feedback: str | None = None
    next_command: str = ""
    footer: FooterInfo = Field(default_factory=FooterInfo)


class DoneRequest(_Model):
    workspace: str | None = None
    chapter: int | None = None
    force: bool = False


class DoneResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    chapter: int
    title: str = ""
    status: str = "todo"
    right: int = 0
    mastery: float = 0.0
    allowed: bool = False
    forced: bool = False
    policy_label_zh: str = ""
    policy_label_en: str = ""
    advance_mastery: float = 0.0
    next_chapter: int | None = None
    next_title: str | None = None
    all_done: bool = False
    footer: FooterInfo = Field(default_factory=FooterInfo)


class NoteRequest(_Model):
    workspace: str | None = None
    text: str
    type: Literal["summary", "confusion", "note"] = "note"
    chapter: int | None = None


class NoteResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    chapter: int
    type: str = "note"
    next_command: str = ""
    footer: FooterInfo = Field(default_factory=FooterInfo)


class MistakeItem(_Model):
    qid: str
    chapter: int | None = None
    count: int = 1
    note: str = ""
    question: QuestionRecord | None = None


class MistakesResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    items: list[MistakeItem] = Field(default_factory=list)
    footer: FooterInfo = Field(default_factory=FooterInfo)


class CheatsheetRequest(_Model):
    workspace: str | None = None
    out: str | None = None


class CheatsheetResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    path: str = ""
    content: str = ""
    written: bool = False
    denied_reason: str | None = None
    footer: FooterInfo = Field(default_factory=FooterInfo)


# ------------------------------------------------------------------ goto / graph / permissions
class GotoRequest(_Model):
    workspace: str | None = None
    chapter: int
    restart: bool = False
    force: bool = False


class PrereqGapView(_Model):
    kp: str
    chapter: int
    title: str
    mastery: float
    threshold: float


class GotoResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    chapter: int
    title: str = ""
    switched: bool = False
    blocked: bool = False
    forced: bool = False
    gap: PrereqGapView | None = None
    next_command: str = ""
    footer: FooterInfo = Field(default_factory=FooterInfo)


class GraphRequest(_Model):
    workspace: str | None = None
    add_prereq: list[str] = Field(default_factory=list)
    remove_prereq: list[str] = Field(default_factory=list)
    rebuild: bool = False


class GraphNodeView(_Model):
    kp: str
    title: str
    mastery: float = 0.0
    unlocked: bool = True
    prerequisites: list[dict[str, str]] = Field(default_factory=list)


class GraphResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    nodes: list[GraphNodeView] = Field(default_factory=list)
    edge_count: int = 0
    stats: dict[str, Any] = Field(default_factory=dict)
    edits: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    changed: bool = False
    footer: FooterInfo = Field(default_factory=FooterInfo)


class PermissionView(_Model):
    id: str
    kind: str
    access: list[str] = Field(default_factory=list)
    value: str = ""
    note: str = ""


class PermissionRequest(_Model):
    workspace: str | None = None
    action: Literal["list", "grant", "revoke"] = "list"
    target: str | None = None
    capability: str | None = None
    read_only: bool = False
    note: str = ""


class PermissionsResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    action: str = "list"
    grants: list[PermissionView] = Field(default_factory=list)
    message_code: str = ""
    message_params: dict[str, Any] = Field(default_factory=dict)
    grant: PermissionView | None = None
    removed: bool = False


# ------------------------------------------------------------------ costs
class SessionCost(_Model):
    session_id: str
    command: str = "agent"
    model: str = "unknown"
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0


class CommandCost(_Model):
    command: str
    sessions: int = 0
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0


class CostsResult(_Model):
    ok: bool = True
    exit_code: int = 0
    workspace: str
    sessions: list[SessionCost] = Field(default_factory=list)
    commands: list[CommandCost] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    input_price_per_mtok: float = 0.0
    output_price_per_mtok: float = 0.0
    log_files: list[str] = Field(default_factory=list)
    currency: str = "USD"
