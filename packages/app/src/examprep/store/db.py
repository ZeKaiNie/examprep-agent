"""数据库连接：异步引擎、SQLite WAL、Alembic 迁移、会话工厂。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event, inspect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

DB_FILE = "examprep.db"
MIGRATIONS = Path(__file__).parent / "migrations"
INITIAL_REVISION = "0001"  # P3a 结束时的表结构


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
        self._migrated = False
        self._migrate_lock = asyncio.Lock()

    @classmethod
    def in_dir(cls, data_dir: str | Path) -> Database:
        """数据目录下的 SQLite 文件（目录不存在则创建）。"""
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return cls(sqlite_url(path / DB_FILE))

    async def migrate(self) -> None:
        """把表结构升级到最新迁移（同一进程里对同一个库只做一次）。"""
        if self._migrated:
            return
        async with self._migrate_lock:
            if self._migrated:
                return
            async with self.engine.begin() as conn:
                await conn.run_sync(upgrade_schema)
            self._migrated = True

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()


def alembic_config(connection: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade_schema(connection: Connection) -> None:
    """升级到 head。P3b 之前用 create_all 建的库没有 alembic_version 表：先标记为初始版本再升级。"""
    cfg = alembic_config(connection)
    tables = set(inspect(connection).get_table_names())
    if "course" in tables and "alembic_version" not in tables:
        command.stamp(cfg, INITIAL_REVISION)
    command.upgrade(cfg, "head")


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
