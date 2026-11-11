"""事件存储接口（R7）。只追加；seq 沿会话谱系连续；支持从任意 seq 分叉。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class StoredEvent:
    session_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class SessionInfo:
    id: str
    parent_id: str | None
    fork_at_seq: int | None
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])


class SessionNotFoundError(KeyError):
    pass


class EventStore(Protocol):
    async def create_session(
        self, *, metadata: dict[str, Any] | None = None, session_id: str | None = None
    ) -> SessionInfo: ...

    async def get_session(self, session_id: str) -> SessionInfo | None: ...

    async def append(self, session_id: str, type: str, payload: dict[str, Any]) -> StoredEvent:
        """追加一个事件，返回带 seq 的记录。"""
        ...

    async def events(self, session_id: str, *, upto_seq: int | None = None) -> list[StoredEvent]:
        """会话的完整事件（含祖先会话在分叉点之前的事件），按 seq 升序。"""
        ...

    async def last_seq(self, session_id: str) -> int: ...

    async def fork(
        self, session_id: str, at_seq: int, *, metadata: dict[str, Any] | None = None
    ) -> SessionInfo:
        """从 ``at_seq``（含）处分叉出新会话。"""
        ...

    async def close(self) -> None: ...
