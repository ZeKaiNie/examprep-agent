"""Alembic 迁移（R6）与学习租约（R5.2）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text

from examprep.learner import lease
from examprep.learner.records import LearnerRecords
from examprep.store import Database
from examprep.store import models as m
from examprep.store.db import utcnow


def _diff(conn: Connection) -> list[Any]:
    return list(compare_metadata(MigrationContext.configure(conn), m.Base.metadata))


def _columns(conn: Connection, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


async def test_migrations_match_models(tmp_path: Path) -> None:
    db = Database.in_dir(tmp_path)
    await db.migrate()
    await db.migrate()  # 幂等
    async with db.engine.connect() as conn:
        assert await conn.run_sync(_diff) == []
        version = (await conn.execute(text("select version_num from alembic_version"))).scalar()
    assert version == "0002"
    await db.close()


async def test_legacy_create_all_database_is_stamped_and_upgraded(tmp_path: Path) -> None:
    """P3b 之前用 create_all 建的库：没有 alembic_version 表、也没有租约列。升级后数据还在。"""
    db = Database.in_dir(tmp_path)
    async with db.engine.begin() as conn:
        await conn.run_sync(m.Base.metadata.create_all)
        await conn.execute(text("alter table enrollment drop column lock_owner"))
        await conn.execute(text("alter table enrollment drop column lock_until"))
        await conn.execute(
            text(
                "insert into course (id, tenant_id, name, root, language, created_at, updated_at) "
                "values ('c1', 'local', '数据结构', '/x', 'zh', '2026-01-01', '2026-01-01')"
            )
        )
    await db.migrate()
    async with db.engine.connect() as conn:
        assert {"lock_owner", "lock_until"} <= await conn.run_sync(_columns, "enrollment")
        assert (await conn.execute(text("select name from course"))).scalar() == "数据结构"
        assert await conn.run_sync(_diff) == []
    await db.close()


# ---------------------------------------------------------------- 租约


@pytest.fixture
async def enr(tmp_path: Path) -> AsyncIterator[tuple[Database, str]]:
    db = Database.in_dir(tmp_path)
    await db.migrate()
    async with db.session() as s:
        t = utcnow()
        s.add(m.Course(id="c1", name="课", root="/x", created_at=t, updated_at=t))
        await s.commit()
    row = await LearnerRecords(db).enrollment("c1")
    yield db, row.id
    await db.close()


async def test_acquire_renew_release(enr: tuple[Database, str]) -> None:
    db, eid = enr
    assert await lease.acquire(db, eid, "cli")
    assert await lease.acquire(db, eid, "cli")  # 自己可以重复获取
    assert not await lease.acquire(db, eid, "web")  # 别人拿不到
    held = await lease.holder(db, eid)
    assert held is not None
    assert held[0] == "cli"
    assert await lease.renew(db, eid, "cli")
    assert not await lease.renew(db, eid, "web")
    await lease.release(db, eid, "web")  # 只能放自己的
    assert not await lease.acquire(db, eid, "web")
    await lease.release(db, eid, "cli")
    assert await lease.holder(db, eid) is None
    assert await lease.acquire(db, eid, "web")


async def test_expired_lease_can_be_taken_over(enr: tuple[Database, str]) -> None:
    """持有方崩溃（不再续约）：到期后别人接手，原持有方续约失败。"""
    db, eid = enr
    t0 = utcnow()
    assert await lease.acquire(db, eid, "crashed", now=lambda: t0 - timedelta(minutes=5))
    assert await lease.holder(db, eid) is None  # 已过期
    assert await lease.acquire(db, eid, "web")
    assert not await lease.renew(db, eid, "crashed")


async def test_save_enrollment_does_not_touch_lease(enr: tuple[Database, str]) -> None:
    db, eid = enr
    rec = LearnerRecords(db)
    stale = await rec.enrollment("c1")  # 拿租约之前读到的对象
    assert await lease.acquire(db, eid, "cli")
    stale.current_chapter = 3
    await rec.save_enrollment(stale)
    held = await lease.holder(db, eid)
    assert held is not None
    assert held[0] == "cli"


async def test_lease_context_renews_and_releases(enr: tuple[Database, str]) -> None:
    db, eid = enr
    async with lease.Lease(db, eid, "cli", every=0.01) as held:
        first = await lease.holder(db, eid)
        await asyncio.sleep(0.05)
        later = await lease.holder(db, eid)
        assert first is not None
        assert later is not None
        assert later[1] >= first[1]  # 续约了
        with pytest.raises(lease.LeaseHeldError, match="另一个窗口"):
            await lease.Lease(db, eid, "web").start()
        assert not held.lost
    assert await lease.holder(db, eid) is None


async def test_lease_lost_when_taken_over(enr: tuple[Database, str]) -> None:
    db, eid = enr
    held = lease.Lease(db, eid, "cli", every=0.01)
    await held.start()
    await lease.release(db, eid, "cli")
    assert await lease.acquire(db, eid, "web")
    await asyncio.sleep(0.05)
    assert held.lost
    await held.stop()
    assert (await lease.holder(db, eid) or ("", None))[0] == "web"  # stop 不会放掉别人的
