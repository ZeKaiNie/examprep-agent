"""任务表：入队、领取（租约）、完成、失败、过期回收（R5.2、R5.3）。

本地模式只有一个导入进程写库；租约用于识别“上次导入中途崩溃”的任务并把它们重新放回队列。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import utcnow
from .models import LOCAL_TENANT, Job

LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3

Now = Callable[[], datetime]


class IngestBusyError(RuntimeError):
    """同一课程有另一次导入正在进行（存在未过期租约的任务）。"""


async def reclaim(
    session: AsyncSession, course_id: str, *, now: Now = utcnow, tenant: str = LOCAL_TENANT
) -> int:
    """回收过期租约的 running 任务；存在未过期的 running 任务时抛 IngestBusyError。返回回收数量。"""
    t = now()
    rows = (
        await session.scalars(
            select(Job).where(Job.tenant_id == tenant, Job.course_id == course_id, Job.status == "running")
        )
    ).all()
    reclaimed = 0
    for job in rows:
        if job.lease_until is not None and job.lease_until > t:
            raise IngestBusyError(f"课程 {course_id} 正在导入中（任务 {job.rel_path} 的租约未过期）")
        job.status = "pending"
        job.lease_until = None
        job.updated_at = t
        reclaimed += 1
    return reclaimed


async def enqueue(
    session: AsyncSession,
    course_id: str,
    rel_path: str,
    sha256: str,
    *,
    now: Now = utcnow,
    tenant: str = LOCAL_TENANT,
) -> Job:
    """同一 (文件, 内容哈希) 只有一个任务；已存在则重置为 pending（尝试次数保留）。"""
    t = now()
    job = await session.scalar(
        select(Job).where(
            Job.tenant_id == tenant,
            Job.course_id == course_id,
            Job.type == "parse",
            Job.rel_path == rel_path,
            Job.sha256 == sha256,
        )
    )
    if job is None:
        job = Job(
            tenant_id=tenant,
            course_id=course_id,
            type="parse",
            rel_path=rel_path,
            sha256=sha256,
            status="pending",
            attempts=0,
            created_at=t,
            updated_at=t,
        )
        session.add(job)
    elif job.status != "pending":
        job.status = "pending"
        job.error = None
        job.updated_at = t
    return job


def claim(job: Job, *, now: Now = utcnow, lease: timedelta = LEASE, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """领取任务：置 running 并设租约。尝试次数已达上限时置 failed 并返回 False。"""
    t = now()
    if job.attempts >= max_attempts:
        job.status = "failed"
        job.error = f"已尝试 {job.attempts} 次，不再重试（{job.error or '未知错误'}）"
        job.updated_at = t
        return False
    job.status = "running"
    job.attempts += 1
    job.lease_until = t + lease
    job.updated_at = t
    return True


def complete(job: Job, *, now: Now = utcnow) -> None:
    job.status = "done"
    job.lease_until = None
    job.error = None
    job.updated_at = now()


def fail(job: Job, error: str, *, now: Now = utcnow) -> None:
    job.status = "failed"
    job.lease_until = None
    job.error = error
    job.updated_at = now()
