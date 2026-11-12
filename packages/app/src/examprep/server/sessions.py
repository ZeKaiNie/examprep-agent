"""服务端的学习会话管理（R4、R5）。

- 每门课在内存里最多一个 StudySession，配一把 asyncio.Lock：同一时间只处理一轮，处理中再发消息直接拒绝（409）。
- 打开会话时取学习租约（跨进程互斥，见 learner.lease），闲置回收和服务关闭时释放。
- 一轮在后台任务里跑，事件进队列，HTTP 响应只是订阅者：浏览器断开、刷新页面都不会把一轮打断在中间，
  这一轮照常跑完、落库，事件写进对话记录，重新打开能看到。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from examprep_llm import CancelToken, Gateway
from examprep_runtime.session import EventStore

from ..learner.lease import PROCESS_OWNER, RENEW_EVERY, Lease
from ..learner.records import LearnerRecords
from ..settings import CompactionPolicy
from ..store import Database
from ..study.events import Notice, StudyEvent, TutorDelta
from ..study.session import StudySession

log = logging.getLogger(__name__)

UI_EVENT = "ui"  # 会话事件：给界面看的对话记录 {"role": "student", "text"} / {"role": "agent", "event"}
IDLE_SECONDS = 30 * 60
TurnKind = Literal["open", "message"]


class TurnBusyError(RuntimeError):
    """这门课的上一轮还在处理。"""


@dataclass(slots=True)
class Live:
    session: StudySession
    lease: Lease
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = 0.0
    task: asyncio.Task[None] | None = None

    @property
    def busy(self) -> bool:
        return self.lock.locked()


class SessionManager:
    def __init__(
        self,
        db: Database,
        events: EventStore,
        gateway: Callable[[], Gateway],
        *,
        compaction: CompactionPolicy | None = None,
        owner: str = PROCESS_OWNER,
        idle_seconds: float = IDLE_SECONDS,
        renew_every: float = RENEW_EVERY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.events = events
        self._gateway = gateway
        self.compaction = compaction or CompactionPolicy()
        self.owner = owner
        self.idle_seconds = idle_seconds
        self.renew_every = renew_every
        self.clock = clock
        self._live: dict[str, Live] = {}
        self._opening = asyncio.Lock()

    # ------------------------------------------------------------ 打开与关闭

    def live(self, course_id: str) -> Live | None:
        return self._live.get(course_id)

    def busy(self, course_id: str) -> bool:
        live = self._live.get(course_id)
        return live is not None and live.busy

    async def get(self, course_id: str) -> Live:
        """取或打开会话。拿不到租约抛 LeaseHeldError；没配置模型抛 SettingsError。"""
        async with self._opening:
            live = self._live.get(course_id)
            if live is not None and live.lease.lost and not live.busy:
                await self._close(course_id)  # 租约被别的进程接手了：丢掉旧会话，重新取
                live = None
            if live is not None:
                return live
            gw = self._gateway()
            tutor = gw.chain(role="tutor")[0]
            enr = await LearnerRecords(self.db).enrollment(course_id)
            lease = Lease(self.db, enr.id, self.owner, every=self.renew_every)
            await lease.start()
            try:
                session = await StudySession.open(
                    self.db,
                    gw,
                    course_id,
                    events=self.events,
                    context_window=gw.model_info(tutor).context_window,
                    compaction=self.compaction,
                )
            except BaseException:
                await lease.stop()
                raise
            live = Live(session=session, lease=lease, last_used=self.clock())
            self._live[course_id] = live
            return live

    async def close(self, course_id: str) -> None:
        """关闭会话并释放租约；有一轮正在处理时抛 TurnBusyError（导入前调用）。"""
        if self.busy(course_id):
            raise TurnBusyError("这门课正在讲解，等这一轮结束再导入。")
        await self._close(course_id)

    async def _close(self, course_id: str) -> None:
        live = self._live.pop(course_id, None)
        if live is not None:
            await live.lease.stop()

    async def reap_idle(self) -> list[str]:
        """关闭闲置太久的会话，返回关掉的课程 id。"""
        now = self.clock()
        idle = [
            cid for cid, lv in self._live.items() if not lv.busy and now - lv.last_used > self.idle_seconds
        ]
        for cid in idle:
            await self._close(cid)
        return idle

    async def shutdown(self) -> None:
        for live in list(self._live.values()):
            live.session.cancel.cancel("server shutdown")
            if live.task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(live.task), timeout=10)
        for cid in list(self._live):
            await self._close(cid)

    # ------------------------------------------------------------ 一轮

    async def run(self, course_id: str, kind: TurnKind, text: str = "") -> AsyncIterator[StudyEvent]:
        """开始一轮并返回事件流。轮次在后台任务里跑；调用方中途不再读取，轮次也会跑完。"""
        live = await self.get(course_id)
        if live.busy:
            raise TurnBusyError("上一条还在处理，请稍等。")
        await live.lock.acquire()
        queue: asyncio.Queue[StudyEvent | None] = asyncio.Queue()
        live.session.cancel = CancelToken()
        live.task = asyncio.create_task(self._turn(live, kind, text, queue))
        return _drain(queue)

    def cancel(self, course_id: str) -> bool:
        live = self._live.get(course_id)
        if live is None or not live.busy:
            return False
        live.session.cancel.cancel("cancelled by user")
        return True

    async def _turn(
        self, live: Live, kind: TurnKind, text: str, queue: asyncio.Queue[StudyEvent | None]
    ) -> None:
        session = live.session
        try:
            if kind == "open":
                it = session.start()
            else:
                await self._record(session, {"role": "student", "text": text})
                it = session.handle(text)
            async for ev in it:
                if kind == "message" and not isinstance(ev, TutorDelta):
                    await self._record(session, {"role": "agent", "event": ev.model_dump(mode="json")})
                queue.put_nowait(ev)
        except Exception as exc:  # 不让一轮里的意外把服务弄挂；告诉学生，记日志
            log.exception("学习轮次出错")
            note = Notice(level="error", text=f"处理时出错：{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await self._record(session, {"role": "agent", "event": note.model_dump(mode="json")})
            queue.put_nowait(note)
        finally:
            queue.put_nowait(None)
            live.last_used = self.clock()
            live.task = None
            live.lock.release()

    async def _record(self, session: StudySession, payload: dict[str, Any]) -> None:
        sid = session.enr.session_id
        if sid is not None:
            await self.events.append(sid, UI_EVENT, payload)

    async def transcript(self, course_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """最近的对话记录。会话没打开时也能读（从选课记录找到会话 id）。"""
        live = self._live.get(course_id)
        if live is not None:
            sid = live.session.enr.session_id
        else:
            enr = await LearnerRecords(self.db).find_enrollment(course_id)
            sid = enr.session_id if enr is not None else None
        if sid is None or await self.events.get_session(sid) is None:
            return []
        rows = [e for e in await self.events.events(sid) if e.type == UI_EVENT]
        return [{"seq": e.seq, **e.payload} for e in rows[-limit:]]


async def _drain(queue: asyncio.Queue[StudyEvent | None]) -> AsyncIterator[StudyEvent]:
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item
