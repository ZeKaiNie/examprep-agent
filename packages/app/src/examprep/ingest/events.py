"""导入进度事件与导入报告（R5.4）。CLI 直接打印，P3 转成 SSE。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Stage = Literal["parse", "build", "crops", "evidence", "save"]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChapterSummary(_Event):
    n: int
    title: str
    sources: list[str]
    questions: int


class InjectionHit(_Event):
    file: str
    pages: list[int]


class IngestReport(_Event):
    course_id: str
    name: str
    root: str
    language: str
    files: dict[str, int]  # 按文件类型计数
    chapters: list[ChapterSummary]
    questions: int
    answered: int
    figures: int
    crops: int
    scan_pages: int
    evidence: int
    parsed: int  # 本次解析的文件数
    reused: int  # 未变化、直接复用的文件数
    removed: int
    failed: int
    warnings: list[str]
    injection: list[InjectionHit]
    seconds: float


class IngestStarted(_Event):
    type: Literal["ingest_started"] = "ingest_started"
    course_id: str
    name: str
    files: int
    new: int
    changed: int
    unchanged: int
    removed: int


class FileStarted(_Event):
    type: Literal["file_started"] = "file_started"
    rel: str


class FileDone(_Event):
    type: Literal["file_done"] = "file_done"
    rel: str
    kind: str
    pages: int
    figures: int
    seconds: float


class FileFailed(_Event):
    type: Literal["file_failed"] = "file_failed"
    rel: str
    error: str


class StageStarted(_Event):
    type: Literal["stage_started"] = "stage_started"
    stage: Stage


class StageDone(_Event):
    type: Literal["stage_done"] = "stage_done"
    stage: Stage
    detail: str = ""


class IngestDone(_Event):
    type: Literal["ingest_done"] = "ingest_done"
    report: IngestReport


IngestEvent = Annotated[
    IngestStarted | FileStarted | FileDone | FileFailed | StageStarted | StageDone | IngestDone,
    Field(discriminator="type"),
]
