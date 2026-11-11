from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from examprep_llm import AssistantMessage, TextBlock, UserMessage
from examprep_llm.testing import FakeTurn
from examprep_runtime import Agent
from examprep_runtime.session import (
    EventStore,
    InMemoryEventStore,
    SessionNotFoundError,
    SessionRecorder,
    SQLiteEventStore,
    load_messages,
    project_messages,
)


@pytest.fixture(params=["memory", "sqlite"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[EventStore]:
    s: EventStore = InMemoryEventStore() if request.param == "memory" else SQLiteEventStore(tmp_path / "s.db")
    yield s
    await s.close()


async def test_append_and_seq(store: EventStore) -> None:
    info = await store.create_session(metadata={"course": "概率论"})
    assert info.metadata == {"course": "概率论"}
    assert info.parent_id is None
    assert await store.last_seq(info.id) == 0
    e1 = await store.append(info.id, "message", {"x": 1})
    e2 = await store.append(info.id, "run_end", {"reason": "completed"})
    assert (e1.seq, e2.seq) == (1, 2)
    events = await store.events(info.id)
    assert [(e.seq, e.type, e.payload) for e in events] == [
        (1, "message", {"x": 1}),
        (2, "run_end", {"reason": "completed"}),
    ]
    assert [e.seq for e in await store.events(info.id, upto_seq=1)] == [1]
    assert await store.get_session("missing") is None
    with pytest.raises(SessionNotFoundError):
        await store.append("missing", "x", {})
    with pytest.raises(ValueError, match="已存在"):
        await store.create_session(session_id=info.id)


async def test_concurrent_appends_unique_seq(store: EventStore) -> None:
    info = await store.create_session()
    results = await asyncio.gather(*(store.append(info.id, "e", {"i": i}) for i in range(50)))
    assert sorted(r.seq for r in results) == list(range(1, 51))
    assert [e.seq for e in await store.events(info.id)] == list(range(1, 51))


async def test_fork_lineage(store: EventStore) -> None:
    root = await store.create_session()
    for i in range(4):
        await store.append(root.id, "e", {"i": i})  # seq 1..4
    child = await store.fork(root.id, 2, metadata={"why": "换一种讲法"})
    assert (child.parent_id, child.fork_at_seq) == (root.id, 2)
    assert await store.last_seq(child.id) == 2
    c3 = await store.append(child.id, "e", {"i": "c"})
    assert c3.seq == 3  # seq 沿谱系连续
    assert [(e.session_id == root.id, e.payload["i"]) for e in await store.events(child.id)] == [
        (True, 0),
        (True, 1),
        (False, "c"),
    ]
    grandchild = await store.fork(child.id, 3)
    await store.append(grandchild.id, "e", {"i": "g"})
    assert [e.payload["i"] for e in await store.events(grandchild.id)] == [0, 1, "c", "g"]
    assert [e.payload["i"] for e in await store.events(grandchild.id, upto_seq=1)] == [0]
    # 原会话不受影响
    assert [e.payload["i"] for e in await store.events(root.id)] == [0, 1, 2, 3]
    # 从开头分叉
    empty = await store.fork(root.id, 0)
    assert await store.events(empty.id) == []
    for bad in (-1, 5):
        with pytest.raises(ValueError, match="分叉点"):
            await store.fork(root.id, bad)


async def test_sqlite_append_only_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    store = SQLiteEventStore(path)
    assert store.journal_mode == "wal"
    info = await store.create_session()
    await store.append(info.id, "e", {})
    await store.close()
    await store.close()  # 幂等
    with pytest.raises(RuntimeError, match="已关闭"):
        await store.append(info.id, "e", {})

    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE events SET type = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM events")
    conn.close()

    # 重新打开后数据仍在
    reopened = SQLiteEventStore(path)
    assert len(await reopened.events(info.id)) == 1
    await reopened.close()


async def test_recorder_and_projection(store: EventStore, make_agent: Any) -> None:
    info = await store.create_session()
    recorder = SessionRecorder(store, info.id)
    agent, _ = make_agent([FakeTurn(text="第一次回答"), FakeTurn(text="第二种讲法")])
    async for ev in agent.run("讲讲贝叶斯"):
        await recorder.record(ev)
    events = await store.events(info.id)
    assert [e.type for e in events] == ["message", "message", "run_end"]
    assert events[-1].payload["reason"] == "completed"
    restored = await load_messages(store, info.id)
    assert restored == agent.messages

    # 从用户提问处分叉，换一种讲法：新会话只含提问
    fork = await store.fork(info.id, 1)
    history = await load_messages(store, fork.id)
    assert [m.role for m in history] == ["user"]
    agent2 = Agent(agent.llm, role="tutor", messages=history)
    recorder2 = SessionRecorder(store, fork.id)
    async for ev in agent2.run():
        await recorder2.record(ev)
    forked = await load_messages(store, fork.id)
    assert isinstance(forked[-1], AssistantMessage)
    assert forked[-1].text == "第二种讲法"
    assert (await load_messages(store, info.id))[-1] == agent.messages[-1]


def test_project_ignores_other_events() -> None:
    from datetime import UTC, datetime

    from examprep_runtime.session import StoredEvent, message_payload

    msg = UserMessage(content=[TextBlock(text="hi")])
    now = datetime.now(UTC)
    events = [
        StoredEvent("s", 1, "message", message_payload(msg), now),
        StoredEvent("s", 2, "state_transition", {"to": "TEACH"}, now),
    ]
    assert project_messages(events) == [msg]
