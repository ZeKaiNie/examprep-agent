"""课程目录：查找课程、列出课程、加载证据索引。"""

from __future__ import annotations

import os

from sqlalchemy import or_, select

from .knowledge.evidence import EvidenceChunk, EvidenceIndex
from .store import Database
from .store import models as m


class CourseNotFoundError(LookupError):
    pass


def _abspath(key: str) -> str:
    return os.path.abspath(key)  # 纯字符串运算，不访问磁盘


async def list_courses(db: Database, *, tenant: str = m.LOCAL_TENANT) -> list[m.Course]:
    await db.migrate()
    async with db.session() as s:
        rows = await s.scalars(
            select(m.Course).where(m.Course.tenant_id == tenant).order_by(m.Course.created_at)
        )
        return list(rows.all())


async def find_course(db: Database, key: str, *, tenant: str = m.LOCAL_TENANT) -> m.Course:
    """按 id、名称或资料目录查找课程；名称重复时报错。"""
    await db.migrate()
    root = _abspath(key)
    async with db.session() as s:
        rows = (
            await s.scalars(
                select(m.Course).where(
                    m.Course.tenant_id == tenant,
                    or_(m.Course.id == key, m.Course.name == key, m.Course.root == root),
                )
            )
        ).all()
    if not rows:
        raise CourseNotFoundError(f"找不到课程：{key}（先运行 examprep ingest）")
    if len(rows) > 1:
        ids = "、".join(f"{r.name}({r.id[:8]})" for r in rows)
        raise CourseNotFoundError(f"有多门课程匹配 {key}：{ids}；请用课程 id")
    return rows[0]


async def load_evidence(db: Database, course_id: str, *, tenant: str = m.LOCAL_TENANT) -> EvidenceIndex:
    async with db.session() as s:
        rows = (
            await s.scalars(
                select(m.Evidence)
                .where(m.Evidence.tenant_id == tenant, m.Evidence.course_id == course_id)
                .order_by(m.Evidence.id)
            )
        ).all()
    return EvidenceIndex(
        EvidenceChunk(
            id=r.evidence_id,
            rel=r.rel_path,
            page=r.page,
            chapter=r.chapter_n,
            ordinal=r.ordinal,
            text=r.text,
            char_start=r.char_start,
            char_end=r.char_end,
            tokens=r.tokens,
        )
        for r in rows
    )
