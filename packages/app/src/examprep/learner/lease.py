"""学习租约（R5.2）：同一门课同一时间只能在一个进程里学。

命令行 ``examprep study`` 和 ``examprep serve`` 都会写同一份学习进度；两边同时学同一门课，进度和会话状态会互相覆盖。
租约存在选课记录上（``lock_owner`` + ``lock_until``），用一条带条件的 UPDATE 原子获取：

    UPDATE enrollment SET lock_owner = :me, lock_until = :t
    WHERE id = :id AND (lock_owner IS NULL OR lock_owner = :me OR lock_until < :now)

影响 1 行就拿到了。持有方定期续约；进程崩溃后租约到期，别的进程就能接手。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, update

from ..store import Database
from ..store import models as m
from ..store.db import utcnow
from ..store.jobs import Now

TTL = timedelta(minutes=2)
RENEW_EVERY = 30.0  # 秒

# 本进程的租约持有者 id：进程号便于排查，uuid 保证重启后不同
PROCESS_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


class LeaseHeldError(RuntimeError):
    """这门课正在别的地方学（另一个命令行窗口或网页服务）。"""


async def acquire(
    db: Database, enrollment_id: str, owner: str = PROCESS_OWNER, *, ttl: timedelta = TTL, now: Now = utcnow
) -> bool:
    t = now()
    async with db.session() as s:
        res = await s.execute(
            update(m.Enrollment)
            .where(
                m.Enrollment.id == enrollment_id,
                or_(
                    m.Enrollment.lock_owner.is_(None),
                    m.Enrollment.lock_owner == owner,
                    m.Enrollment.lock_until < t,
                ),
            )
            .values(lock_owner=owner, lock_until=t + ttl)
        )
        await s.commit()
    return _rowcount(res) == 1


async def renew(
    db: Database, enrollment_id: str, owner: str = PROCESS_OWNER, *, ttl: timedelta = TTL, now: Now = utcnow
) -> bool:
    """只续自己的租约；租约已被别人接手时返回 False。"""
    async with db.session() as s:
        res = await s.execute(
            update(m.Enrollment)
            .where(m.Enrollment.id == enrollment_id, m.Enrollment.lock_owner == owner)
            .values(lock_until=now() + ttl)
        )
        await s.commit()
    return _rowcount(res) == 1


async def release(db: Database, enrollment_id: str, owner: str = PROCESS_OWNER) -> None:
    async with db.session() as s:
        await s.execute(
            update(m.Enrollment)
            .where(m.Enrollment.id == enrollment_id, m.Enrollment.lock_owner == owner)
            .values(lock_owner=None, lock_until=None)
        )
        await s.commit()


async def holder(db: Database, enrollment_id: str, *, now: Now = utcnow) -> tuple[str, datetime] | None:
    """当前有效的持有者（未过期），没有则 None。"""
    async with db.session() as s:
        row = await s.get(m.Enrollment, enrollment_id)
    if row is None or row.lock_owner is None or row.lock_until is None or row.lock_until < now():
        return None
    return row.lock_owner, row.lock_until


class Lease:
    """持有一门课的租约，并在后台定期续约。``async with Lease(db, enrollment_id):``"""

    def __init__(
        self, db: Database, enrollment_id: str, owner: str = PROCESS_OWNER, *, every: float = RENEW_EVERY
    ) -> None:
        self.db = db
        self.enrollment_id = enrollment_id
        self.owner = owner
        self.every = every
        self.lost = False  # 续约失败（被别人接手）
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Lease:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if not await acquire(self.db, self.enrollment_id, self.owner):
            raise LeaseHeldError("这门课正在另一个窗口或网页里学习。请先关掉那边，或等两分钟让它自动释放。")
        self._task = asyncio.create_task(self._renew_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await release(self.db, self.enrollment_id, self.owner)

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            if not await renew(self.db, self.enrollment_id, self.owner):
                self.lost = True
                return


def _rowcount(result: object) -> int:
    count = getattr(result, "rowcount", 0)
    return count if isinstance(count, int) else 0
