"""数据模型（设计文档 03 第 4 节的资料侧 + 任务表）。

所有表带 ``tenant_id``（本地模式固定为 ``local``）。时间一律存 UTC（不带时区信息的 naive datetime）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

LOCAL_TENANT = "local"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class Course(Base):
    __tablename__ = "course"
    __table_args__ = (UniqueConstraint("tenant_id", "root"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT, index=True)
    name: Mapped[str] = mapped_column(String(200))
    root: Mapped[str] = mapped_column(Text)  # 资料目录的绝对路径
    language: Mapped[str] = mapped_column(String(8), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class Material(Base):
    __tablename__ = "material"
    __table_args__ = (UniqueConstraint("tenant_id", "course_id", "rel_path"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT, index=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), default="")  # 解析成功后才写入；失败为空串，下次会重试
    kind: Mapped[str] = mapped_column(String(16), default="other")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / removed
    error: Mapped[str | None] = mapped_column(Text, default=None)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    empty_pages: Mapped[list[Any]] = mapped_column(default=list)
    pages_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class Page(Base):
    __tablename__ = "page"
    __table_args__ = (UniqueConstraint("material_id", "n"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT)
    material_id: Mapped[str] = mapped_column(ForeignKey("material.id", ondelete="CASCADE"), index=True)
    n: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    is_scan: Mapped[bool] = mapped_column(Boolean, default=False)
    width: Mapped[float | None] = mapped_column(default=None)
    height: Mapped[float | None] = mapped_column(default=None)
    # PDF 页面布局：图框与文字框，供题目裁图使用
    layout: Mapped[dict[str, Any] | None] = mapped_column(default=None)


class Chapter(Base):
    __tablename__ = "chapter"
    __table_args__ = (UniqueConstraint("course_id", "n"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    n: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[Any]] = mapped_column(default=list)
    figures: Mapped[list[Any]] = mapped_column(default=list)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("course_id", "evidence_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    evidence_id: Mapped[str] = mapped_column(String(16))  # E + 10 位哈希
    material_id: Mapped[str] = mapped_column(ForeignKey("material.id", ondelete="CASCADE"))
    rel_path: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)
    chapter_n: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(default=None)
    char_end: Mapped[int | None] = mapped_column(default=None)
    tokens: Mapped[int] = mapped_column(Integer, default=0)


class Figure(Base):
    __tablename__ = "figure"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[str] = mapped_column(ForeignKey("material.id", ondelete="CASCADE"), index=True)
    rel_path: Mapped[str] = mapped_column(Text)
    page: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), default="figure")  # figure / question / answer
    path: Mapped[str] = mapped_column(Text)  # 相对课程数据目录
    box: Mapped[list[Any] | None] = mapped_column(default=None)
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)
    question_id: Mapped[str | None] = mapped_column(String(16), default=None)


class Question(Base):
    __tablename__ = "question"
    __table_args__ = (UniqueConstraint("course_id", "qid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    qid: Mapped[str] = mapped_column(String(16))
    number: Mapped[int | None] = mapped_column(default=None)
    label: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(16))
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[list[Any]] = mapped_column(default=list)
    answer: Mapped[str | None] = mapped_column(Text, default=None)
    points: Mapped[int | None] = mapped_column(default=None)
    chapter_n: Mapped[int | None] = mapped_column(default=None)
    chapter_guessed: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[dict[str, Any]] = mapped_column(default=dict)
    answer_source: Mapped[dict[str, Any] | None] = mapped_column(default=None)
    is_exam: Mapped[bool] = mapped_column(Boolean, default=False)


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (UniqueConstraint("tenant_id", "course_id", "type", "rel_path", "sha256"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default=LOCAL_TENANT)
    course_id: Mapped[str] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(16), default="parse")
    rel_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / running / done / failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
