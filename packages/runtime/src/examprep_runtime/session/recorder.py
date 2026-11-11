"""把 Agent 事件写入事件存储。"""

from __future__ import annotations

from examprep_llm import Message

from ..events import AgentEnd, AgentEvent, MessageEnd
from .projection import MESSAGE_EVENT, RUN_END_EVENT, message_payload, project_messages
from .store import EventStore, StoredEvent


class SessionRecorder:
    """订阅 Agent 事件流：``MessageEnd`` → ``message``，``AgentEnd`` → ``run_end``；其余增量事件不落库。"""

    def __init__(self, store: EventStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id

    async def record(self, event: AgentEvent) -> StoredEvent | None:
        if isinstance(event, MessageEnd):
            return await self.store.append(self.session_id, MESSAGE_EVENT, message_payload(event.message))
        if isinstance(event, AgentEnd):
            return await self.store.append(
                self.session_id,
                RUN_END_EVENT,
                {"run_id": event.run_id, "reason": event.reason, "error": event.error},
            )
        return None


async def load_messages(store: EventStore, session_id: str, *, upto_seq: int | None = None) -> list[Message]:
    return project_messages(await store.events(session_id, upto_seq=upto_seq))
