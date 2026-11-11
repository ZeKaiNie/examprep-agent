"""学习记录：选课、章节进度、只追加的作答、错题与已验证、笔记。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from examprep.learner.records import AttemptInput, LearnerRecords
from examprep.store import Course, Database, utcnow


@pytest.fixture
async def rec(tmp_path: Path) -> AsyncIterator[LearnerRecords]:
    db = Database.in_dir(tmp_path)
    await db.create_all()
    async with db.session() as s:
        t = utcnow()
        s.add(Course(id="c1", name="DS", root="/m", created_at=t, updated_at=t))
        await s.commit()
    yield LearnerRecords(db)
    await db.close()


def attempt(
    qid: str, verdict: str, *, chapter: int = 1, ref: bool = True, kind: str = "question"
) -> AttemptInput:
    return AttemptInput(
        kind=kind, qid=qid, chapter_n=chapter, answer="x", verdict=verdict, grader="rule", has_reference=ref
    )


async def test_enrollment_is_unique_and_saved(rec: LearnerRecords) -> None:
    a = await rec.enrollment("c1")
    b = await rec.enrollment("c1")
    assert a.id == b.id
    assert (a.mode, a.start_level, a.current_chapter, a.user_id) == ("cram", "zero", 1, "me")
    a.current_chapter = 3
    a.session_state = {"state": "PRACTICE", "pending_qid": "q002"}
    a.exam_date = "2026-12-01"
    await rec.save_enrollment(a)
    again = await rec.enrollment("c1")
    assert again.current_chapter == 3
    assert again.session_state["pending_qid"] == "q002"
    assert again.exam_date == "2026-12-01"


async def test_progress_rows_created_and_saved(rec: LearnerRecords) -> None:
    enr = await rec.enrollment("c1")
    rows = await rec.progress(enr.id, [1, 2, 3])
    assert sorted(rows) == [1, 2, 3]
    assert rows[1].status == "todo"
    rows[1].parts_done, rows[1].parts_total, rows[1].status = 2, 4, "teaching"
    await rec.save_progress(rows[1])
    again = await rec.progress(enr.id, [1, 2, 3, 4])
    assert (again[1].parts_done, again[1].status) == (2, "teaching")
    assert 4 in again


async def test_mistakes_and_verified_follow_last_attempt(rec: LearnerRecords) -> None:
    enr = await rec.enrollment("c1")
    await rec.record(enr.id, attempt("q001", "wrong"))
    await rec.record(enr.id, attempt("q002", "partial", chapter=2))
    await rec.record(enr.id, attempt("q003", "unsure"))
    await rec.record(enr.id, attempt("c", "right", kind="check"))
    assert await rec.open_mistakes(enr.id) == {"q001", "q002"}
    assert not await rec.chapter_verified(enr.id, 1)
    await rec.record(enr.id, attempt("q001", "right"))
    assert await rec.open_mistakes(enr.id) == {"q002"}
    assert await rec.chapter_verified(enr.id, 1)
    await rec.record(enr.id, attempt("q009", "right", chapter=2, ref=False))
    assert not await rec.chapter_verified(enr.id, 2)  # 没有参考答案的题不算验证
    assert len(await rec.attempts(enr.id)) == 5
    assert len(await rec.attempts(enr.id, kind="check")) == 1


async def test_notes(rec: LearnerRecords) -> None:
    enr = await rec.enrollment("c1")
    await rec.add_note(enr.id, "summary", "顺序表 O(1) 访问", 1)
    await rec.add_note(enr.id, "confusion", "为什么是 n-i+1", 1)
    await rec.add_note(enr.id, "confusion", "循环队列判满", 2)
    assert len(await rec.notes(enr.id)) == 3
    assert [n.text for n in await rec.notes(enr.id, type="confusion", chapter_n=1)] == ["为什么是 n-i+1"]
