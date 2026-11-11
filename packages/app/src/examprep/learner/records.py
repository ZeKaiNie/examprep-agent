"""学习记录的读写。作答记录只追加，“错题”“已验证”都由作答记录推出（R1）。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select

from ..store import Database
from ..store import models as m
from ..store.db import utcnow

RIGHT = "right"
OPEN_VERDICTS = ("wrong", "partial")


@dataclass(slots=True)
class AttemptInput:
    kind: str  # question / check
    qid: str | None
    chapter_n: int | None
    answer: str
    verdict: str
    grader: str
    score: float | None = None
    error_type: str | None = None
    reason: str | None = None
    hints_used: int = 0
    has_reference: bool = False


class LearnerRecords:
    def __init__(self, db: Database, *, tenant: str = m.LOCAL_TENANT, user: str = m.LOCAL_USER) -> None:
        self.db = db
        self.tenant = tenant
        self.user = user

    # ------------------------------------------------------------ 选课

    async def enrollment(self, course_id: str) -> m.Enrollment:
        """取或建选课记录。"""
        async with self.db.session() as s:
            row = await s.scalar(
                select(m.Enrollment).where(
                    m.Enrollment.tenant_id == self.tenant,
                    m.Enrollment.user_id == self.user,
                    m.Enrollment.course_id == course_id,
                )
            )
            if row is None:
                t = utcnow()
                row = m.Enrollment(
                    id=uuid.uuid4().hex,
                    tenant_id=self.tenant,
                    user_id=self.user,
                    course_id=course_id,
                    session_state={},
                    created_at=t,
                    updated_at=t,
                )
                s.add(row)
                await s.commit()
            return row

    async def save_enrollment(self, enr: m.Enrollment) -> None:
        async with self.db.session() as s:
            row = await s.get(m.Enrollment, enr.id)
            assert row is not None
            row.mode = enr.mode
            row.start_level = enr.start_level
            row.exam_date = enr.exam_date
            row.profile = enr.profile
            row.current_chapter = enr.current_chapter
            row.session_id = enr.session_id
            row.session_state = dict(enr.session_state)
            row.updated_at = utcnow()
            await s.commit()

    # ------------------------------------------------------------ 章节进度

    async def progress(self, enrollment_id: str, chapters: Sequence[int]) -> dict[int, m.ChapterProgress]:
        """各章进度；缺的章节补建为 todo。"""
        async with self.db.session() as s:
            rows = {
                r.chapter_n: r
                for r in (
                    await s.scalars(
                        select(m.ChapterProgress).where(m.ChapterProgress.enrollment_id == enrollment_id)
                    )
                ).all()
            }
            added = False
            for n in chapters:
                if n not in rows:
                    row = m.ChapterProgress(
                        tenant_id=self.tenant, enrollment_id=enrollment_id, chapter_n=n, updated_at=utcnow()
                    )
                    s.add(row)
                    rows[n] = row
                    added = True
            if added:
                await s.commit()
            return rows

    async def save_progress(self, row: m.ChapterProgress) -> None:
        async with self.db.session() as s:
            cur = await s.get(m.ChapterProgress, row.id)
            assert cur is not None
            cur.parts_done = row.parts_done
            cur.parts_total = row.parts_total
            cur.status = row.status
            cur.check_failures = row.check_failures
            cur.practiced = row.practiced
            cur.updated_at = utcnow()
            await s.commit()

    # ------------------------------------------------------------ 作答

    async def record(self, enrollment_id: str, a: AttemptInput) -> m.Attempt:
        async with self.db.session() as s:
            row = m.Attempt(
                tenant_id=self.tenant,
                user_id=self.user,
                enrollment_id=enrollment_id,
                kind=a.kind,
                qid=a.qid,
                chapter_n=a.chapter_n,
                answer=a.answer,
                verdict=a.verdict,
                score=a.score,
                grader=a.grader,
                error_type=a.error_type,
                reason=a.reason,
                hints_used=a.hints_used,
                has_reference=a.has_reference,
                created_at=utcnow(),
            )
            s.add(row)
            await s.commit()
            return row

    async def attempts(self, enrollment_id: str, *, kind: str = "question") -> list[m.Attempt]:
        async with self.db.session() as s:
            rows = await s.scalars(
                select(m.Attempt)
                .where(m.Attempt.enrollment_id == enrollment_id, m.Attempt.kind == kind)
                .order_by(m.Attempt.id)
            )
            return list(rows.all())

    async def last_verdicts(self, enrollment_id: str) -> dict[str, str]:
        """每道题最近一次的判定（不含 unsure）。"""
        out: dict[str, str] = {}
        for a in await self.attempts(enrollment_id):
            if a.qid and a.verdict != "unsure":
                out[a.qid] = a.verdict
        return out

    async def open_mistakes(self, enrollment_id: str) -> set[str]:
        """最近一次判为错或部分对的题。"""
        return {q for q, v in (await self.last_verdicts(enrollment_id)).items() if v in OPEN_VERDICTS}

    async def chapter_verified(self, enrollment_id: str, chapter_n: int) -> bool:
        """本章有一道带参考答案的材料题答对过。"""
        return any(
            a.chapter_n == chapter_n and a.verdict == RIGHT and a.has_reference
            for a in await self.attempts(enrollment_id)
        )

    # ------------------------------------------------------------ 笔记

    async def add_note(
        self, enrollment_id: str, type: str, text: str, chapter_n: int | None = None
    ) -> m.Note:
        async with self.db.session() as s:
            row = m.Note(
                tenant_id=self.tenant,
                enrollment_id=enrollment_id,
                chapter_n=chapter_n,
                type=type,
                text=text,
                created_at=utcnow(),
            )
            s.add(row)
            await s.commit()
            return row

    async def notes(
        self, enrollment_id: str, *, type: str | None = None, chapter_n: int | None = None
    ) -> list[m.Note]:
        async with self.db.session() as s:
            q = select(m.Note).where(m.Note.enrollment_id == enrollment_id)
            if type is not None:
                q = q.where(m.Note.type == type)
            if chapter_n is not None:
                q = q.where(m.Note.chapter_n == chapter_n)
            return list((await s.scalars(q.order_by(m.Note.id))).all())
