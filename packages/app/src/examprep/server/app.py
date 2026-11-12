"""本机 Web 服务（P3b）：课程、导入、检索、原文、学习。接口说明见 .kiro/specs/p3b-server/design.md。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from examprep_llm import Gateway
from examprep_runtime.session import SQLiteEventStore

from .. import __version__
from ..catalog import CourseNotFoundError, find_course, list_courses, load_evidence
from ..ingest.events import IngestEvent
from ..ingest.figures import render_region
from ..ingest.pipeline import COURSES_DIR, ingest
from ..learner.lease import LeaseHeldError
from ..settings import SESSIONS_DB, CompactionPolicy, SettingsError, load_compaction, load_settings
from ..store import Database
from ..store import models as m
from ..store.jobs import IngestBusyError
from .security import LocalGuard, Principal, current_user
from .sessions import SessionManager, TurnBusyError, TurnKind
from .sse import error_frame, frames, sse_response

log = logging.getLogger(__name__)

RENDER_DIR = "render"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # P3c 构建产物
REAP_EVERY = 60.0
MAX_SCALE = 4.0

User = Annotated[Principal, Depends(current_user)]


class IngestBody(BaseModel):
    path: str = Field(min_length=1, description="本机资料目录")
    name: str | None = None
    lang: str | None = Field(default=None, pattern="^(zh|en)$")


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def _settings_gateway(data_dir: Path) -> Callable[[], Gateway]:
    cached: list[Gateway] = []

    def make() -> Gateway:
        if not cached:
            cached.append(Gateway.from_config(load_settings(data_dir)))
        return cached[0]

    return make


def create_app(
    data_dir: str | Path,
    *,
    gateway: Gateway | None = None,
    compaction: CompactionPolicy | None = None,
    extra_hosts: tuple[str, ...] = (),
    web_dir: Path | None = None,
    reap_every: float = REAP_EVERY,
) -> FastAPI:
    """``gateway`` 给测试注入假模型；不给时按数据目录的配置（或环境变量）在第一次学习时创建。"""
    data = Path(data_dir)
    db = Database.in_dir(data)
    store = SQLiteEventStore(data / SESSIONS_DB)
    gateway_factory: Callable[[], Gateway] = (
        (lambda: gateway) if gateway is not None else _settings_gateway(data)
    )
    policy = compaction or load_compaction(data)
    manager = SessionManager(db, store, gateway_factory, compaction=policy)
    render_lock = asyncio.Lock()  # pdfium 不是线程安全的：同一时间只渲染一页

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await db.migrate()

        async def reaper() -> None:
            while True:
                await asyncio.sleep(reap_every)
                with contextlib.suppress(Exception):
                    await manager.reap_idle()

        task = asyncio.create_task(reaper())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await manager.shutdown()
            await store.close()
            if gateway is None:
                with contextlib.suppress(SettingsError):
                    await gateway_factory().aclose()
            await db.close()

    app = FastAPI(title="ExamPrep Agent", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.db = db

    async def course_or_404(cid: str, user: Principal) -> m.Course:
        try:
            return await find_course(db, cid, tenant=user.tenant)
        except CourseNotFoundError as exc:
            raise HTTPException(404, str(exc)) from None

    def gateway_or_503() -> Gateway:
        try:
            return gateway_factory()
        except SettingsError as exc:
            raise HTTPException(503, str(exc)) from None

    # ------------------------------------------------------------ 基本信息

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        try:
            gateway_factory()
            configured = True
        except SettingsError:
            configured = False
        return {"version": __version__, "model_configured": configured}

    @app.get("/api/settings")
    async def settings(_user: User) -> dict[str, Any]:
        try:
            gw = gateway_factory()
            roles: dict[str, list[str]] = dict(gw.roles)
            reasoning: dict[str, str] = dict(gw.reasoning)
        except SettingsError:
            roles, reasoning = {}, {}
        return {
            "roles": roles,  # 只有模型名，不含 Key
            "reasoning": reasoning,
            "compaction": {"auto": policy.auto, "ratio": policy.ratio, "max_tokens": policy.max_tokens},
        }

    # ------------------------------------------------------------ 课程

    @app.get("/api/courses")
    async def courses(user: User) -> list[dict[str, Any]]:
        rows = await list_courses(db, tenant=user.tenant)
        return [_course_row(c) for c in rows]

    @app.get("/api/courses/{cid}")
    async def course_detail(cid: str, user: User) -> dict[str, Any]:
        course = await course_or_404(cid, user)
        async with db.session() as s:
            chapters = (
                await s.scalars(
                    select(m.Chapter).where(m.Chapter.course_id == course.id).order_by(m.Chapter.n)
                )
            ).all()
            counted = await s.execute(
                select(m.Question.chapter_n, func.count())
                .where(m.Question.course_id == course.id)
                .group_by(m.Question.chapter_n)
            )
            per_chapter: dict[int | None, int] = {}
            for n, c in counted.all():
                per_chapter[n] = c
            materials = (
                await s.scalars(
                    select(m.Material)
                    .where(m.Material.course_id == course.id, m.Material.status == "active")
                    .order_by(m.Material.rel_path)
                )
            ).all()
            figures = await s.scalar(
                select(func.count()).select_from(m.Figure).where(m.Figure.course_id == course.id)
            )
        return {
            **_course_row(course),
            "chapters": [
                {"n": c.n, "title": c.title, "sources": list(c.sources), "questions": per_chapter.get(c.n, 0)}
                for c in chapters
            ],
            "questions": sum(per_chapter.values()),
            "figures": figures or 0,
            "materials": [
                {"file": x.rel_path, "kind": x.kind, "pages": x.pages_count, "error": x.error}
                for x in materials
            ],
        }

    @app.post("/api/courses/ingest")
    async def ingest_course(body: IngestBody, user: User) -> Response:
        root = await asyncio.to_thread(lambda: Path(body.path).expanduser())
        if not await asyncio.to_thread(root.is_dir):
            raise HTTPException(400, f"不是一个存在的目录：{body.path}")
        name, lang = body.name, body.lang
        with contextlib.suppress(CourseNotFoundError):
            existing = await find_course(db, str(await asyncio.to_thread(root.resolve)), tenant=user.tenant)
            try:
                await manager.close(existing.id)  # 资料要变了：先关掉内存里的会话
            except TurnBusyError as exc:
                raise HTTPException(409, str(exc)) from None
        queue: asyncio.Queue[IngestEvent | BaseException | None] = asyncio.Queue()

        async def job() -> None:
            try:
                await ingest(db, data, root, name=name, lang=lang, workers=None, on_event=queue.put_nowait)
            except BaseException as exc:
                queue.put_nowait(exc)
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(job())
        # 等第一个事件：导入还没开始就失败（同一课程正在导入等）时，直接返回错误状态码，而不是一条 200 的流
        first = await queue.get()
        if isinstance(first, BaseException) or first is None:
            await task
            if isinstance(first, IngestBusyError):
                raise HTTPException(409, str(first))
            raise HTTPException(400, f"导入失败：{first}")
        head: IngestEvent = first

        async def events() -> AsyncIterator[IngestEvent]:
            item: IngestEvent | BaseException | None = head
            while item is not None:
                if isinstance(item, BaseException):
                    raise item
                yield item
                item = await queue.get()

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in frames(events()):
                    yield chunk
            except Exception as exc:  # 流已经开始：用 error 事件告诉前端
                yield error_frame(f"导入失败：{exc}")

        return sse_response(stream())

    @app.get("/api/courses/{cid}/search")
    async def search(
        cid: str,
        user: User,
        q: Annotated[str, Query(min_length=1)],
        k: Annotated[int, Query(ge=1, le=50)] = 5,
        chapter: int | None = None,
    ) -> list[dict[str, Any]]:
        course = await course_or_404(cid, user)
        index = await load_evidence(db, course.id, tenant=user.tenant)
        return [
            {
                "id": h.chunk.id,
                "file": h.chunk.rel,
                "page": h.chunk.page,
                "chapter": h.chunk.chapter,
                "score": round(h.score, 4),
                "text": h.chunk.text,
            }
            for h in index.search(q, k, chapter=chapter)
        ]

    # ------------------------------------------------------------ 原文

    @app.get("/api/courses/{cid}/page")
    async def page(
        cid: str, user: User, file: Annotated[str, Query(min_length=1)], page: Annotated[int, Query(ge=1)]
    ) -> dict[str, Any]:
        course = await course_or_404(cid, user)
        async with db.session() as s:
            mat = await s.scalar(
                select(m.Material).where(m.Material.course_id == course.id, m.Material.rel_path == file)
            )
            if mat is None:
                raise HTTPException(404, f"资料里没有这个文件：{file}")
            row = await s.scalar(select(m.Page).where(m.Page.material_id == mat.id, m.Page.n == page))
            if row is None:
                raise HTTPException(404, f"{file} 没有第 {page} 页")
            evidence = (
                await s.scalars(
                    select(m.Evidence)
                    .where(
                        m.Evidence.course_id == course.id,
                        m.Evidence.rel_path == file,
                        m.Evidence.page == page,
                    )
                    .order_by(m.Evidence.ordinal)
                )
            ).all()
        return {
            "file": file,
            "page": page,
            "pages": mat.pages_count,
            "text": row.text,
            "is_scan": row.is_scan,
            "width": row.width,
            "height": row.height,
            "renderable": file.lower().endswith(".pdf"),
            "evidence": [
                {
                    "id": e.evidence_id,
                    "chapter": e.chapter_n,
                    "start": e.char_start,
                    "end": e.char_end,
                    "text": e.text,
                }
                for e in evidence
            ],
        }

    @app.get("/api/courses/{cid}/render")
    async def render(
        cid: str,
        user: User,
        file: Annotated[str, Query(min_length=1)],
        page: Annotated[int, Query(ge=1)],
        scale: Annotated[float, Query(gt=0, le=MAX_SCALE)] = 1.5,
    ) -> FileResponse:
        course = await course_or_404(cid, user)
        if not file.lower().endswith(".pdf"):
            raise HTTPException(400, "只能渲染 PDF 页面")
        src = inside(Path(course.root), file)
        if src is None or not src.is_file():
            raise HTTPException(404, f"找不到资料文件：{file}")
        async with db.session() as s:
            mat = await s.scalar(
                select(m.Material).where(m.Material.course_id == course.id, m.Material.rel_path == file)
            )
        if mat is None or page > mat.pages_count:
            raise HTTPException(404, f"{file} 没有第 {page} 页")
        out = data / COURSES_DIR / course.id / RENDER_DIR / f"{mat.id}_{mat.sha256[:8]}_p{page}_{scale:g}.png"
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            async with render_lock:
                await asyncio.to_thread(render_region, str(src), page, None, str(out), scale)
        return FileResponse(out, media_type="image/png")

    @app.get("/api/courses/{cid}/assets/{path:path}")
    async def asset(cid: str, path: str, user: User) -> FileResponse:
        course = await course_or_404(cid, user)
        target = inside(data / COURSES_DIR / course.id, path)
        if target is None or not target.is_file():
            raise HTTPException(404, f"找不到图片：{path}")
        return FileResponse(target)

    # ------------------------------------------------------------ 学习

    async def turn(cid: str, user: Principal, kind: TurnKind, text: str = "") -> Response:
        course = await course_or_404(cid, user)
        gateway_or_503()
        try:
            events = await manager.run(course.id, kind, text)
        except TurnBusyError as exc:
            raise HTTPException(409, str(exc)) from None
        except LeaseHeldError as exc:
            raise HTTPException(423, str(exc)) from None
        return sse_response(frames(events))

    @app.post("/api/study/{cid}/open")
    async def study_open(cid: str, user: User) -> Response:
        return await turn(cid, user, "open")

    @app.post("/api/study/{cid}/message")
    async def study_message(cid: str, body: MessageBody, user: User) -> Response:
        return await turn(cid, user, "message", body.text.strip())

    @app.post("/api/study/{cid}/compact")
    async def study_compact(cid: str, user: User) -> Response:
        return await turn(cid, user, "message", "压缩上下文")

    @app.post("/api/study/{cid}/cancel")
    async def study_cancel(cid: str, user: User) -> dict[str, bool]:
        course = await course_or_404(cid, user)
        return {"cancelled": manager.cancel(course.id)}

    @app.get("/api/study/{cid}/state")
    async def study_state(cid: str, user: User) -> dict[str, Any]:
        course = await course_or_404(cid, user)
        live = manager.live(course.id)
        if live is None:
            return {"open": False, "busy": False}
        s = live.session
        return {
            "open": True,
            "busy": live.busy,
            "chapter": s.chapter,
            "state": s.state,
            "awaiting": s.awaiting(),
            "context_tokens": s.segment_tokens(),
            "compact_limit": s.compact_limit(),
            "auto_compact": s.compaction.auto,
        }

    @app.get("/api/study/{cid}/transcript")
    async def study_transcript(
        cid: str, user: User, limit: Annotated[int, Query(ge=1, le=2000)] = 200
    ) -> list[dict[str, Any]]:
        course = await course_or_404(cid, user)
        return await manager.transcript(course.id, limit)

    # ------------------------------------------------------------ 前端

    web = web_dir if web_dir is not None else WEB_DIR
    if (web / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return PLACEHOLDER

    app.add_middleware(LocalGuard, extra_hosts=extra_hosts)
    return app


def inside(base: Path, rel: str) -> Path | None:
    """``base / rel``，但必须仍在 base 里面（拒绝 ``..``、绝对路径、盘符）。"""
    root = base.resolve()
    target = (root / rel).resolve()
    return target if target == root or root in target.parents else None


def _course_row(c: m.Course) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "language": c.language,
        "root": c.root,
        "updated_at": c.updated_at.isoformat(),
    }


PLACEHOLDER = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>ExamPrep Agent</title>
<body style="font-family:system-ui;max-width:40em;margin:4em auto;line-height:1.6">
<h1>ExamPrep Agent 服务已启动</h1>
<p>网页界面还没有构建（P3c）。现在可以用接口：<a href="/docs">/docs</a> 有全部接口说明。</p>
</body></html>"""
