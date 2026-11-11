"""SQLite 事件存储（WAL）。

单连接 + 线程锁，所有操作经 ``asyncio.to_thread`` 执行，不阻塞事件循环。追加在 ``BEGIN IMMEDIATE`` 事务里
计算 seq，多协程并发追加不会重号。``events`` 表有触发器禁止 UPDATE / DELETE，保证只追加。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from .store import SessionInfo, SessionNotFoundError, StoredEvent

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES sessions(id),
    fork_at_seq INTEGER,
    created_at  TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
    session_id  TEXT NOT NULL REFERENCES sessions(id),
    seq         INTEGER NOT NULL,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _session(row: sqlite3.Row) -> SessionInfo:
    return SessionInfo(
        id=row["id"],
        parent_id=row["parent_id"],
        fork_at_seq=row["fork_at_seq"],
        created_at=datetime.fromisoformat(row["created_at"]),
        metadata=cast(dict[str, Any], json.loads(row["metadata"])),
    )


def _event(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        session_id=row["session_id"],
        seq=row["seq"],
        type=row["type"],
        payload=cast(dict[str, Any], json.loads(row["payload"])),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SQLiteEventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._closed = False

    @property
    def journal_mode(self) -> str:
        return str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])

    async def _run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            with self._lock:
                if self._closed:
                    raise RuntimeError("存储已关闭")
                return fn(self._conn)

        return await asyncio.to_thread(locked)

    # ------------------------------------------------------------ 会话

    async def create_session(
        self, *, metadata: dict[str, Any] | None = None, session_id: str | None = None
    ) -> SessionInfo:
        sid = session_id or uuid.uuid4().hex
        meta = json.dumps(metadata or {}, ensure_ascii=False)

        def op(conn: sqlite3.Connection) -> SessionInfo:
            try:
                conn.execute(
                    "INSERT INTO sessions(id, parent_id, fork_at_seq, created_at, metadata) VALUES (?, NULL, NULL, ?, ?)",
                    (sid, _now(), meta),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"会话已存在：{sid}") from exc
            return self._get(conn, sid)

        return await self._run(op)

    @staticmethod
    def _get(conn: sqlite3.Connection, sid: str) -> SessionInfo:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            raise SessionNotFoundError(sid)
        return _session(row)

    async def get_session(self, session_id: str) -> SessionInfo | None:
        def op(conn: sqlite3.Connection) -> SessionInfo | None:
            try:
                return self._get(conn, session_id)
            except SessionNotFoundError:
                return None

        return await self._run(op)

    @classmethod
    def _last_seq(cls, conn: sqlite3.Connection, sid: str) -> int:
        info = cls._get(conn, sid)
        row = conn.execute("SELECT MAX(seq) FROM events WHERE session_id = ?", (sid,)).fetchone()
        if row[0] is not None:
            return int(row[0])
        return info.fork_at_seq or 0

    async def last_seq(self, session_id: str) -> int:
        return await self._run(lambda conn: self._last_seq(conn, session_id))

    # ------------------------------------------------------------ 事件

    async def append(self, session_id: str, type: str, payload: dict[str, Any]) -> StoredEvent:
        data = json.dumps(payload, ensure_ascii=False)

        def op(conn: sqlite3.Connection) -> StoredEvent:
            conn.execute("BEGIN IMMEDIATE")
            try:
                seq = self._last_seq(conn, session_id) + 1
                created = _now()
                conn.execute(
                    "INSERT INTO events(session_id, seq, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, seq, type, data, created),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return StoredEvent(
                session_id=session_id,
                seq=seq,
                type=type,
                payload=cast(dict[str, Any], json.loads(data)),
                created_at=datetime.fromisoformat(created),
            )

        return await self._run(op)

    async def events(self, session_id: str, *, upto_seq: int | None = None) -> list[StoredEvent]:
        def op(conn: sqlite3.Connection) -> list[StoredEvent]:
            # 先沿 parent 链收集 (会话, 上限)，再从最老的祖先开始读
            chain: list[tuple[str, int | None]] = []
            sid = session_id
            limit = upto_seq
            while True:
                info = self._get(conn, sid)
                chain.append((sid, limit))
                if info.parent_id is None or info.fork_at_seq is None:
                    break
                limit = info.fork_at_seq if limit is None else min(limit, info.fork_at_seq)
                sid = info.parent_id
            out: list[StoredEvent] = []
            for sid_, lim in reversed(chain):
                if lim is None:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE session_id = ? ORDER BY seq", (sid_,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM events WHERE session_id = ? AND seq <= ? ORDER BY seq", (sid_, lim)
                    ).fetchall()
                out.extend(_event(r) for r in rows)
            return out

        return await self._run(op)

    async def fork(
        self, session_id: str, at_seq: int, *, metadata: dict[str, Any] | None = None
    ) -> SessionInfo:
        sid = uuid.uuid4().hex
        meta = json.dumps(metadata or {}, ensure_ascii=False)

        def op(conn: sqlite3.Connection) -> SessionInfo:
            last = self._last_seq(conn, session_id)
            if not 0 <= at_seq <= last:
                raise ValueError(f"分叉点 {at_seq} 超出范围 [0, {last}]")
            conn.execute(
                "INSERT INTO sessions(id, parent_id, fork_at_seq, created_at, metadata) VALUES (?, ?, ?, ?, ?)",
                (sid, session_id, at_seq, _now(), meta),
            )
            return self._get(conn, sid)

        return await self._run(op)

    async def close(self) -> None:
        def op() -> None:
            with self._lock:
                if not self._closed:
                    self._closed = True
                    self._conn.close()

        await asyncio.to_thread(op)
