"""存储层：建表、WAL、租户字段、任务表的租约与回收。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from examprep.store import Course, Database, Job, Material, utcnow
from examprep.store.jobs import IngestBusyError, claim, complete, enqueue, fail, reclaim


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = Database.in_dir(tmp_path / "data")
    await d.create_all()
    yield d
    await d.close()


async def make_course(db: Database) -> str:
    async with db.session() as s:
        t = utcnow()
        s.add(Course(id="c1", name="数据结构", root="/m", language="zh", created_at=t, updated_at=t))
        await s.commit()
    return "c1"


async def test_tables_wal_and_tenant(db: Database, tmp_path: Path) -> None:
    await make_course(db)
    async with db.session() as s:
        mode = (await s.execute(text("PRAGMA journal_mode"))).scalar()
        assert mode == "wal"
        s.add(Material(id="m1", course_id="c1", rel_path="lec1.pdf", updated_at=utcnow()))
        await s.commit()
        m = await s.scalar(select(Material).where(Material.id == "m1"))
        assert m is not None
        assert m.tenant_id == "local"
        assert m.warnings == []
        assert m.status == "active"
    assert (tmp_path / "data" / "examprep.db").exists()


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 11, 8, 0, 0)

    def __call__(self) -> datetime:
        return self.t


async def test_job_lifecycle_lease_and_reclaim(db: Database) -> None:
    await make_course(db)
    clock = Clock()
    async with db.session() as s:
        job = await enqueue(s, "c1", "lec1.pdf", "aaa", now=clock)
        await s.commit()
        assert job.status == "pending"
        assert claim(job, now=clock)
        assert job.status == "running"
        assert job.attempts == 1
        assert job.lease_until == clock.t + timedelta(minutes=10)
        await s.commit()

        # 租约未过期：另一次导入应被拒绝
        with pytest.raises(IngestBusyError):
            await reclaim(s, "c1", now=clock)
        # 进程崩溃，租约过期后回收
        clock.t += timedelta(minutes=11)
        assert await reclaim(s, "c1", now=clock) == 1
        assert job.status == "pending"
        assert job.lease_until is None
        assert await reclaim(s, "c1", now=clock) == 0

        assert claim(job, now=clock)
        complete(job, now=clock)
        assert job.status == "done"
        # 同一文件同一哈希再次入队：复用同一行并重置为 pending
        again = await enqueue(s, "c1", "lec1.pdf", "aaa", now=clock)
        assert again.id == job.id
        assert again.status == "pending"
        await s.commit()
        assert len((await s.scalars(select(Job))).all()) == 1


async def test_job_failure_and_attempt_cap(db: Database) -> None:
    await make_course(db)
    async with db.session() as s:
        job = await enqueue(s, "c1", "bad.pdf", "bbb")
        for _ in range(3):
            assert claim(job)
            fail(job, "坏文件")
            await enqueue(s, "c1", "bad.pdf", "bbb")
        assert job.attempts == 3
        assert not claim(job)
        assert job.status == "failed"
        assert "不再重试" in (job.error or "")
        await s.commit()
