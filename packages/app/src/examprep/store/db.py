"""数据库连接：异步引擎、SQLite WAL、建表、会话工厂。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base

DB_FILE = "examprep.db"


def utcnow() -> datetime:
    """当前 UTC 时间（naive，与库内存储格式一致）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def sqlite_url(path: str | Path) -> str:
    return f"sqlite+aiosqlite:///{Path(path).as_posix()}"


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @classmethod
    def in_dir(cls, data_dir: str | Path) -> Database:
        """数据目录下的 SQLite 文件（目录不存在则创建）。"""
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return cls(sqlite_url(path / DB_FILE))

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
