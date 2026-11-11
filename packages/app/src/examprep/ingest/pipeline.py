"""导入编排：扫描 → 哈希 → 增量判定 → 并行解析 → 课程级重建 → 落库，全程产生进度事件（R4、R5）。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..knowledge.evidence import EvidenceChunk, build_evidence
from ..store import db as dbmod
from ..store import models as m
from ..store.jobs import Now, claim, complete, enqueue, fail, reclaim
from . import guard
from .chapters import Chapter, build_chapters
from .events import (
    ChapterSummary,
    FileDone,
    FileFailed,
    IngestDone,
    IngestEvent,
    IngestReport,
    IngestStarted,
    InjectionHit,
    StageDone,
    StageStarted,
)
from .extract import Page, Source, list_files, natural_key, rel_path
from .figures import FigureRecord, PdfPageInfo, clear_dir, crop_blocks, safe_stem, segments_for
from .parse import ParsedFile, ParseRequest, layout_from_dict, parse_file
from .questions import QuestionItem, extract_questions, heads
from .text import is_mostly_cjk, shorten

COURSES_DIR = "courses"
CROPS_DIR = "crops"
FIGURES_DIR = "figures"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


HEAVY_EXT = (".pdf", ".pptx", ".docx")
AUTO_POOL_MIN_FILES = 4


def auto_workers(rels: list[str]) -> int:
    """待解析的 PDF/Office 文件不少于 4 个时才开进程池（Windows 上起子进程约需 1 秒）。"""
    heavy = sum(1 for r in rels if r.lower().endswith(HEAVY_EXT))
    if heavy < AUTO_POOL_MIN_FILES:
        return 0
    return max(1, min(4, os.cpu_count() or 1, heavy))


def scan_directory(root: str) -> tuple[str, list[str], dict[str, str]]:
    """返回 (绝对路径, 自然排序的相对路径, {相对路径: sha256})。"""
    root_abs = os.path.abspath(root)
    if not os.path.isdir(root_abs):
        raise FileNotFoundError(f"资料目录不存在：{root_abs}")
    files = [rel_path(root_abs, p) for p in list_files(root_abs)]
    return root_abs, files, {rel: sha256_file(os.path.join(root_abs, rel)) for rel in files}


@dataclass(slots=True)
class _Plan:
    files: list[str]  # 相对路径，自然排序
    sha: dict[str, str]
    to_parse: list[str]
    new: int
    unchanged: int
    removed: list[m.Material]


class Ingestor:
    """把一个资料目录导入为一门课程。同一课程同一时间只能有一次导入。"""

    def __init__(
        self,
        db: dbmod.Database,
        data_dir: str | Path,
        *,
        workers: int | None = 0,
        tenant: str = m.LOCAL_TENANT,
        now: Now = dbmod.utcnow,
    ) -> None:
        self.db = db
        self.data_dir = Path(data_dir)
        self.workers = workers  # 0：当前进程顺序解析；None：按待解析文件自动决定
        self.tenant = tenant
        self.now = now

    def course_dir(self, course_id: str) -> Path:
        return self.data_dir / COURSES_DIR / course_id

    # ------------------------------------------------------------ 入口

    async def run(
        self, root: str | Path, *, name: str | None = None, lang: str | None = None
    ) -> AsyncIterator[IngestEvent]:
        t0 = time.perf_counter()
        root_abs, files, shas = await asyncio.to_thread(scan_directory, str(root))
        await self.db.create_all()

        async with self.db.session() as s:
            course = await self._course(s, root_abs, name)
            await reclaim(s, course.id, now=self.now, tenant=self.tenant)
            plan = await self._plan(s, course.id, files, shas)
            await s.commit()
        yield IngestStarted(
            course_id=course.id,
            name=course.name,
            files=len(files),
            new=plan.new,
            changed=len(plan.to_parse) - plan.new,
            unchanged=plan.unchanged,
            removed=len(plan.removed),
        )

        yield StageStarted(stage="parse")
        parsed = failed = 0
        async for ev in self._parse_all(course.id, root_abs, plan):
            if isinstance(ev, FileDone):
                parsed += 1
            elif isinstance(ev, FileFailed):
                failed += 1
            yield ev
        await self._remove(course.id, plan.removed)
        yield StageDone(stage="parse", detail=f"解析 {parsed}，失败 {failed}，复用 {plan.unchanged}")

        yield StageStarted(stage="build")
        sources, materials, layouts, page_rows = await self._load_sources(course.id, root_abs)
        chapters = build_chapters(sources)
        language = lang or ("zh" if is_mostly_cjk("\n".join(c.text for c in chapters)) else "en")
        sanitize_titles(chapters, language)
        items = extract_questions(sources, chapters)
        yield StageDone(stage="build", detail=f"{len(chapters)} 章，{len(items)} 题")

        yield StageStarted(stage="crops")
        crops_dir = self.course_dir(course.id) / CROPS_DIR
        crops = await asyncio.to_thread(question_crops, items, sources, layouts, root_abs, str(crops_dir))
        yield StageDone(stage="crops", detail=f"{len(crops)} 张")

        yield StageStarted(stage="evidence")
        page_texts = {(src.rel, p.number): p.text for src in sources for p in src.pages}
        chunks = build_evidence(chapters, page_texts)
        yield StageDone(stage="evidence", detail=f"{len(chunks)} 块")

        yield StageStarted(stage="save")
        await self._save(course.id, language, chapters, items, crops, chunks, materials)
        yield StageDone(stage="save")

        report = build_report(
            course=course,
            root=root_abs,
            language=language,
            sources=sources,
            chapters=chapters,
            items=items,
            crops=crops,
            chunks=chunks,
            page_rows=page_rows,
            figures=await self._count_figures(course.id),
            parsed=parsed,
            reused=plan.unchanged,
            removed=len(plan.removed),
            failed=failed,
            seconds=time.perf_counter() - t0,
        )
        yield IngestDone(report=report)

    # ------------------------------------------------------------ 规划

    async def _course(self, s: AsyncSession, root: str, name: str | None) -> m.Course:
        course = await s.scalar(
            select(m.Course).where(m.Course.tenant_id == self.tenant, m.Course.root == root)
        )
        t = self.now()
        if course is None:
            course = m.Course(
                id=uuid.uuid4().hex,
                tenant_id=self.tenant,
                name=name or os.path.basename(root.rstrip("\\/")) or "course",
                root=root,
                created_at=t,
                updated_at=t,
            )
            s.add(course)
        elif name:
            course.name = name
        await s.flush()
        return course

    async def _plan(self, s: AsyncSession, course_id: str, files: list[str], shas: dict[str, str]) -> _Plan:
        rows = (await s.scalars(select(m.Material).where(m.Material.course_id == course_id))).all()
        existing = {r.rel_path: r for r in rows}
        to_parse: list[str] = []
        new = unchanged = 0
        t = self.now()
        for rel in files:
            mat = existing.get(rel)
            if mat is None:
                s.add(
                    m.Material(
                        id=uuid.uuid4().hex,
                        tenant_id=self.tenant,
                        course_id=course_id,
                        rel_path=rel,
                        updated_at=t,
                    )
                )
                to_parse.append(rel)
                new += 1
            elif mat.status != "active" or not mat.sha256 or mat.sha256 != shas[rel]:
                mat.status = "active"
                to_parse.append(rel)
            else:
                unchanged += 1
        present = set(files)
        removed = [r for r in rows if r.status == "active" and r.rel_path not in present]
        return _Plan(files, shas, to_parse, new, unchanged, removed)

    # ------------------------------------------------------------ 解析

    async def _parse_all(self, course_id: str, root: str, plan: _Plan) -> AsyncIterator[IngestEvent]:
        requests: list[tuple[ParseRequest, str]] = []
        async with self.db.session() as s:
            mats = await self._materials(s, course_id)
            for rel in plan.to_parse:
                job = await enqueue(s, course_id, rel, plan.sha[rel], now=self.now, tenant=self.tenant)
                if not claim(job, now=self.now):
                    mat = mats[rel]
                    mat.error = job.error
                    mat.sha256 = ""
                    yield FileFailed(rel=rel, error=job.error or "")
                    continue
                mat_id = mats[rel].id
                fig_dir = self.course_dir(course_id) / FIGURES_DIR / mat_id
                req = ParseRequest(root, rel, str(fig_dir), f"{FIGURES_DIR}/{mat_id}")
                requests.append((req, plan.sha[rel]))
            await s.commit()

        async for req, sha, result in self._execute(requests):
            async with self.db.session() as s:
                await self._store_file(s, course_id, req.rel, sha, result)
                await s.commit()
            if result.error:
                yield FileFailed(rel=req.rel, error=result.error)
            else:
                yield FileDone(
                    rel=req.rel,
                    kind=result.kind,
                    pages=len(result.pages),
                    figures=len(result.figures),
                    seconds=result.seconds,
                )

    async def _execute(
        self, requests: list[tuple[ParseRequest, str]]
    ) -> AsyncIterator[tuple[ParseRequest, str, ParsedFile]]:
        if not requests:
            return
        workers = self.workers if self.workers is not None else auto_workers([r.rel for r, _ in requests])
        if workers <= 0:
            for req, sha in requests:
                yield req, sha, await asyncio.to_thread(parse_file, req)
            return
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=workers) as pool:

            async def one(req: ParseRequest, sha: str) -> tuple[ParseRequest, str, ParsedFile]:
                return req, sha, await loop.run_in_executor(pool, parse_file, req)

            pending: list[Awaitable[tuple[ParseRequest, str, ParsedFile]]] = [one(r, h) for r, h in requests]
            for fut in asyncio.as_completed(pending):
                yield await fut

    async def _store_file(
        self, s: AsyncSession, course_id: str, rel: str, sha: str, result: ParsedFile
    ) -> None:
        mats = await self._materials(s, course_id)
        mat = mats[rel]
        await s.execute(delete(m.Page).where(m.Page.material_id == mat.id))
        await s.execute(delete(m.Figure).where(m.Figure.material_id == mat.id, m.Figure.kind == "figure"))
        t = self.now()
        mat.kind = result.kind
        mat.error = result.error
        mat.warnings = list(result.warnings)
        mat.empty_pages = list(result.empty_pages)
        mat.pages_count = len(result.pages)
        mat.sha256 = "" if result.error else sha
        mat.status = "active"
        mat.updated_at = t
        for p in result.pages:
            lay = result.layouts.get(p.number)
            s.add(
                m.Page(
                    tenant_id=self.tenant,
                    material_id=mat.id,
                    n=p.number,
                    text=p.text,
                    is_scan=p.scan,
                    width=lay["w"] if lay else None,
                    height=lay["h"] if lay else None,
                    layout=lay,
                )
            )
        for f in result.figures:
            s.add(_figure_row(self.tenant, course_id, mat.id, f))
        job = await s.scalar(
            select(m.Job).where(m.Job.course_id == course_id, m.Job.rel_path == rel, m.Job.sha256 == sha)
        )
        if job is not None:
            if result.error:
                fail(job, result.error, now=self.now)
            else:
                complete(job, now=self.now)

    async def _materials(self, s: AsyncSession, course_id: str) -> dict[str, m.Material]:
        rows = (await s.scalars(select(m.Material).where(m.Material.course_id == course_id))).all()
        return {r.rel_path: r for r in rows}

    async def _remove(self, course_id: str, removed: list[m.Material]) -> None:
        if not removed:
            return
        async with self.db.session() as s:
            for mat in removed:
                row = await s.get(m.Material, mat.id)
                if row is None:
                    continue
                row.status = "removed"
                row.updated_at = self.now()
                await s.execute(delete(m.Page).where(m.Page.material_id == row.id))
                await s.execute(delete(m.Figure).where(m.Figure.material_id == row.id))
                fig_dir = self.course_dir(course_id) / FIGURES_DIR / row.id
                if fig_dir.is_dir():
                    clear_dir(str(fig_dir))
                    fig_dir.rmdir()
            await s.commit()

    # ------------------------------------------------------------ 课程级重建

    async def _load_sources(
        self, course_id: str, root: str
    ) -> tuple[list[Source], dict[str, m.Material], dict[str, dict[int, PdfPageInfo]], list[m.Page]]:
        async with self.db.session() as s:
            mats = [r for r in (await self._materials(s, course_id)).values() if r.status == "active"]
            ids = [r.id for r in mats]
            page_rows = list(
                (await s.scalars(select(m.Page).where(m.Page.material_id.in_(ids)).order_by(m.Page.n))).all()
            )
        by_mat: dict[str, list[m.Page]] = {}
        for p in page_rows:
            by_mat.setdefault(p.material_id, []).append(p)
        sources: list[Source] = []
        layouts: dict[str, dict[int, PdfPageInfo]] = {}
        for mat in sorted(mats, key=lambda r: natural_key(r.rel_path)):
            src = Source(os.path.join(root, mat.rel_path), mat.rel_path, mat.kind)
            src.error = mat.error
            src.warnings = list(mat.warnings or [])
            src.empty_pages = list(mat.empty_pages or [])
            rows = by_mat.get(mat.id, [])
            src.pages = [Page(p.n, p.text, p.is_scan) for p in rows]
            lay = {p.n: layout_from_dict(p.n, p.layout) for p in rows if p.layout}
            if lay:
                layouts[mat.rel_path] = lay
            sources.append(src)
        return sources, {r.rel_path: r for r in mats}, layouts, page_rows

    async def _save(
        self,
        course_id: str,
        language: str,
        chapters: list[Chapter],
        items: list[QuestionItem],
        crops: list[FigureRecord],
        chunks: list[EvidenceChunk],
        materials: dict[str, m.Material],
    ) -> None:
        async with self.db.session() as s:
            for model in (m.Chapter, m.Evidence, m.Question):
                await s.execute(delete(model).where(model.course_id == course_id))
            await s.execute(
                delete(m.Figure).where(m.Figure.course_id == course_id, m.Figure.kind != "figure")
            )
            course = await s.get(m.Course, course_id)
            if course is not None:
                course.language = language
                course.updated_at = self.now()
            for ch in chapters:
                s.add(
                    m.Chapter(
                        tenant_id=self.tenant,
                        course_id=course_id,
                        n=ch.number,
                        title=ch.title,
                        sources=list(ch.sources),
                        figures=list(ch.figures),
                    )
                )
            for c in chunks:
                s.add(
                    m.Evidence(
                        tenant_id=self.tenant,
                        course_id=course_id,
                        evidence_id=c.id,
                        material_id=materials[c.rel].id,
                        rel_path=c.rel,
                        page=c.page,
                        chapter_n=c.chapter,
                        ordinal=c.ordinal,
                        text=c.text,
                        char_start=c.char_start,
                        char_end=c.char_end,
                        tokens=c.tokens,
                    )
                )
            for it in items:
                d = it.to_dict()
                s.add(
                    m.Question(
                        tenant_id=self.tenant,
                        course_id=course_id,
                        qid=it.id,
                        number=it.number,
                        label=it.label,
                        type=it.type,
                        stem=it.question,
                        options=list(it.options),
                        answer=it.answer,
                        points=it.points,
                        chapter_n=it.chapter,
                        chapter_guessed=it.chapter_guessed,
                        source=d["source"],
                        answer_source=d["answer_source"],
                        is_exam=it.source.kind == "exam",
                    )
                )
            for f in crops:
                s.add(_figure_row(self.tenant, course_id, materials[f.file].id, f))
            await s.commit()

    async def _count_figures(self, course_id: str) -> int:
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(m.Figure.id).where(m.Figure.course_id == course_id, m.Figure.kind == "figure")
                )
            ).all()
            return len(rows)


# ---------------------------------------------------------------- 纯函数部分


def _figure_row(tenant: str, course_id: str, material_id: str, f: FigureRecord) -> m.Figure:
    return m.Figure(
        tenant_id=tenant,
        course_id=course_id,
        material_id=material_id,
        rel_path=f.file,
        page=f.page,
        kind=f.kind,
        path=f.path,
        box=f.box,
        width=f.w,
        height=f.h,
        question_id=f.qid,
    )


def sanitize_titles(chapters: list[Chapter], language: str) -> None:
    """章节标题来自文件里的标题行，会不加围栏地显示：可疑的换成“第 N 章”，过长的截断。"""
    for ch in chapters:
        if guard.is_suspicious(ch.title) or guard.neutralize(ch.title) != ch.title:
            ch.title = f"第 {ch.number} 章" if language == "zh" else f"Chapter {ch.number}"
        elif len(ch.title) > 80:
            ch.title = shorten(ch.title, 80)


def question_crops(
    items: list[QuestionItem],
    sources: list[Source],
    layouts: dict[str, dict[int, PdfPageInfo]],
    root: str,
    crops_dir: str,
) -> list[FigureRecord]:
    """裁出包含图的题面与答案区域（移植 Skill cli.attach_question_figures）。"""
    clear_dir(crops_dir)
    by_rel = {s.rel: s for s in sources}
    head_cache: dict[str, list[tuple[int, str, str]]] = {}

    def heads_of(rel: str) -> list[tuple[int, str, str]]:
        if rel not in head_cache:
            head_cache[rel] = heads(by_rel[rel]) if rel in by_rel else []
        return head_cache[rel]

    records: list[FigureRecord] = []
    for it in items:
        for side in ("source", "answer_source"):
            ref = it.source if side == "source" else it.answer_source
            paths: list[str] = []
            if ref is not None and ref.file in layouts:
                hs = heads_of(ref.file)
                pos = next((k for k, h in enumerate(hs) if h[0] == ref.page and h[1] == ref.head), None)
                if pos is not None:
                    infos = layouts[ref.file]
                    segs = segments_for(infos, hs, pos)
                    label = it.id if side == "source" else it.id + "_ans"
                    pdf_path = os.path.join(root, ref.file)
                    paths = crop_blocks(
                        pdf_path, infos, segs, crops_dir, CROPS_DIR, safe_stem(ref.file), label
                    )
                    kind = "question" if side == "source" else "answer"
                    records.extend(
                        FigureRecord(file=ref.file, page=ref.page, path=p, kind=kind, qid=it.id)
                        for p in paths
                    )
            if side == "source":
                it.figures = paths
            else:
                it.answer_figures = paths
    return records


def build_report(
    *,
    course: m.Course,
    root: str,
    language: str,
    sources: list[Source],
    chapters: list[Chapter],
    items: list[QuestionItem],
    crops: list[FigureRecord],
    chunks: list[EvidenceChunk],
    page_rows: list[m.Page],
    figures: int,
    parsed: int,
    reused: int,
    removed: int,
    failed: int,
    seconds: float,
) -> IngestReport:
    kinds: dict[str, int] = {}
    for src in sources:
        kinds[src.kind] = kinds.get(src.kind, 0) + 1
    summaries = [
        ChapterSummary(
            n=c.number,
            title=c.title,
            sources=list(c.sources),
            questions=sum(1 for q in items if q.chapter == c.number),
        )
        for c in chapters
    ]
    warnings: list[str] = []
    injection: list[InjectionHit] = []
    for src in sources:
        if src.error:
            warnings.append(f"{src.rel}: 读取失败（{src.error}）")
        elif "no_text" in src.warnings:
            warnings.append(f"{src.rel}: 没有可抽取的文字（可能是扫描件）")
        warnings.extend(f"{src.rel}: {w}" for w in src.warnings if w.startswith("figures_failed"))
        if src.pages and not src.error:
            flagged = [p.number for p in src.pages if guard.flagged_lines(p.text, limit=1)]
            if flagged:
                injection.append(InjectionHit(file=src.rel, pages=flagged))
    return IngestReport(
        course_id=course.id,
        name=course.name,
        root=root,
        language=language,
        files=dict(sorted(kinds.items())),
        chapters=summaries,
        questions=len(items),
        answered=sum(1 for q in items if q.answer),
        figures=figures,
        crops=len(crops),
        scan_pages=sum(1 for p in page_rows if p.is_scan),
        evidence=len(chunks),
        parsed=parsed,
        reused=reused,
        removed=removed,
        failed=failed,
        warnings=warnings,
        injection=injection,
        seconds=round(seconds, 3),
    )


async def ingest(
    db: dbmod.Database,
    data_dir: str | Path,
    root: str | Path,
    *,
    name: str | None = None,
    lang: str | None = None,
    workers: int | None = 0,
    on_event: Callable[[IngestEvent], None] | None = None,
) -> IngestReport:
    """跑完一次导入并返回报告；``on_event`` 接收全部进度事件。"""
    report: IngestReport | None = None
    async for ev in Ingestor(db, data_dir, workers=workers).run(root, name=name, lang=lang):
        if on_event is not None:
            on_event(ev)
        if isinstance(ev, IngestDone):
            report = ev.report
    assert report is not None
    return report
