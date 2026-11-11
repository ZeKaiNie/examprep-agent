"""内存事件存储（测试与单进程临时会话用）。"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from .store import SessionInfo, SessionNotFoundError, StoredEvent


class InMemoryEventStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._events: dict[str, list[StoredEvent]] = {}

    async def create_session(
        self, *, metadata: dict[str, Any] | None = None, session_id: str | None = None
    ) -> SessionInfo:
        return self._create(session_id or uuid.uuid4().hex, None, None, metadata)

    def _create(
        self, sid: str, parent: str | None, fork_at: int | None, metadata: dict[str, Any] | None
    ) -> SessionInfo:
        if sid in self._sessions:
            raise ValueError(f"会话已存在：{sid}")
        info = SessionInfo(
            id=sid,
            parent_id=parent,
            fork_at_seq=fork_at,
            created_at=datetime.now(UTC),
            metadata=dict(metadata or {}),
        )
        self._sessions[sid] = info
        self._events[sid] = []
        return info

    async def get_session(self, session_id: str) -> SessionInfo | None:
        return self._sessions.get(session_id)

    def _require(self, session_id: str) -> SessionInfo:
        info = self._sessions.get(session_id)
        if info is None:
            raise SessionNotFoundError(session_id)
        return info

    def _last_seq(self, session_id: str) -> int:
        info = self._require(session_id)
        own = self._events[session_id]
        if own:
            return own[-1].seq
        return info.fork_at_seq or 0

    async def last_seq(self, session_id: str) -> int:
        return self._last_seq(session_id)

    async def append(self, session_id: str, type: str, payload: dict[str, Any]) -> StoredEvent:
        seq = self._last_seq(session_id) + 1
        ev = StoredEvent(
            session_id=session_id,
            seq=seq,
            type=type,
            payload=copy.deepcopy(payload),
            created_at=datetime.now(UTC),
        )
        self._events[session_id].append(ev)
        return ev

    async def events(self, session_id: str, *, upto_seq: int | None = None) -> list[StoredEvent]:
        info = self._require(session_id)
        out: list[StoredEvent] = []
        if info.parent_id is not None and info.fork_at_seq is not None:
            limit = info.fork_at_seq if upto_seq is None else min(upto_seq, info.fork_at_seq)
            out.extend(await self.events(info.parent_id, upto_seq=limit))
        out.extend(e for e in self._events[session_id] if upto_seq is None or e.seq <= upto_seq)
        return out

    async def fork(
        self, session_id: str, at_seq: int, *, metadata: dict[str, Any] | None = None
    ) -> SessionInfo:
        last = self._last_seq(session_id)
        if not 0 <= at_seq <= last:
            raise ValueError(f"分叉点 {at_seq} 超出范围 [0, {last}]")
        return self._create(uuid.uuid4().hex, session_id, at_seq, metadata)

    async def close(self) -> None:
        pass
