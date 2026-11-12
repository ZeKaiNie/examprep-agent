"""学习事件。CLI 直接显示，P3b 转成 SSE。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

State = Literal["TEACH", "CHECK", "PRACTICE", "SELF", "DONE"]
TutorKind = Literal["teach", "reteach", "check", "feedback", "hint", "answer", "wrapup"]


class _E(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CitationRef(_E):
    label: str
    evidence_id: str
    file: str
    page: int


class FigureRef(_E):
    file: str
    page: int
    path: str  # 相对课程数据目录


class ChapterRow(_E):
    n: int
    title: str
    status: str
    parts_done: int
    parts_total: int
    questions: int
    correct: int


class StateChanged(_E):
    type: Literal["state"] = "state"
    state: State
    chapter: int


class FiguresShown(_E):
    type: Literal["figures"] = "figures"
    figures: list[FigureRef]


class TutorDelta(_E):
    type: Literal["tutor_delta"] = "tutor_delta"
    text: str


class TutorMessage(_E):
    type: Literal["tutor_message"] = "tutor_message"
    kind: TutorKind
    text: str  # 已渲染引用
    citations: list[CitationRef]
    unknown: list[str]  # 被删除的编造编号


class Verified(_E):
    type: Literal["verified"] = "verified"
    total: int
    supported: int
    partial: int
    unsupported: int
    text: str  # 校验后的最终文本（不支撑的句子加 🟡），前端用它替换流式文本


class QuestionShown(_E):
    type: Literal["question"] = "question"
    qid: str
    chapter: int | None
    qtype: str
    stem: str
    options: list[str]
    points: int | None
    source_file: str
    source_page: int
    figures: list[FigureRef]
    has_answer: bool
    index: int
    total: int


class Graded(_E):
    type: Literal["graded"] = "graded"
    qid: str
    verdict: str  # right / partial / wrong / unsure / skip
    score: float | None
    grader: str  # rule / llm / self
    reason: str | None
    error_type: str | None
    reference: str | None
    reference_source: str | None  # "文件 p.N"
    answer_figures: list[FigureRef]


class Refused(_E):
    type: Literal["refused"] = "refused"
    query: str
    text: str


class NoteSaved(_E):
    type: Literal["note"] = "note"
    note_type: str
    chapter: int | None
    text: str


class ChapterDone(_E):
    type: Literal["chapter_done"] = "chapter_done"
    chapter: int
    status: str  # verified / covered


class CourseDone(_E):
    type: Literal["course_done"] = "course_done"


class ProgressReport(_E):
    type: Literal["progress"] = "progress"
    course: str
    current: int
    mode: str
    days_left: int | None
    chapters: list[ChapterRow]
    open_mistakes: int


class Compacted(_E):
    """上下文压缩：之前的对话换成摘要（ok=False 表示摘要失败，直接开了新对话）。"""

    type: Literal["compacted"] = "compacted"
    manual: bool
    ok: bool
    tokens_before: int
    tokens_after: int
    summary: str


class Suggestions(_E):
    type: Literal["suggestions"] = "suggestions"
    items: list[str]


class Notice(_E):
    type: Literal["notice"] = "notice"
    level: Literal["info", "warning", "error"] = "info"
    text: str


StudyEvent = Annotated[
    StateChanged
    | FiguresShown
    | TutorDelta
    | TutorMessage
    | Verified
    | QuestionShown
    | Graded
    | Refused
    | NoteSaved
    | ChapterDone
    | CourseDone
    | ProgressReport
    | Compacted
    | Suggestions
    | Notice,
    Field(discriminator="type"),
]
