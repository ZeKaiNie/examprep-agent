"""命令行入口：examprep ingest / search / courses（R6）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import __version__
from .catalog import CourseNotFoundError, find_course, list_courses, load_evidence
from .ingest import guard
from .ingest.events import FileDone, FileFailed, IngestEvent, IngestReport
from .ingest.pipeline import ingest
from .ingest.text import shorten
from .store import Database
from .store.jobs import IngestBusyError

KIND_NAMES = {
    "lecture": "课件",
    "notes": "笔记",
    "homework": "作业",
    "exam": "试卷",
    "solution": "答案",
    "figure": "图片",
    "other": "其他",
}


def default_data_dir() -> Path:
    env = os.environ.get("EXAMPREP_HOME")
    return Path(env) if env else Path.home() / ".examprep"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="examprep", description="ExamPrep Agent：基于你自己的资料学习")
    p.add_argument("--version", action="version", version=f"examprep {__version__}")
    p.add_argument(
        "--data-dir", type=Path, default=None, help="数据目录（默认 $EXAMPREP_HOME 或 ~/.examprep）"
    )
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("ingest", help="导入资料目录（再次运行只处理有变化的文件）")
    pi.add_argument("materials", type=Path, help="课程资料目录")
    pi.add_argument("--course", dest="name", default=None, help="课程名称（默认取目录名）")
    pi.add_argument("--lang", choices=["zh", "en"], default=None, help="讲解语言（默认自动判断）")
    pi.add_argument("--workers", type=int, default=None, help="解析进程数（0 表示不开进程池；默认自动）")
    pi.add_argument("--json", action="store_true", help="以 JSON 输出导入报告")
    pi.add_argument("--quiet", action="store_true", help="不显示逐个文件的进度")

    ps = sub.add_parser("search", help="在课程资料中检索")
    ps.add_argument("course", help="课程名称、id 或资料目录")
    ps.add_argument("query", nargs="+", help="检索词")
    ps.add_argument("-k", type=int, default=5, help="返回条数")
    ps.add_argument("--chapter", type=int, default=None, help="只在某一章检索")
    ps.add_argument("--json", action="store_true")

    sub.add_parser("courses", help="列出已导入的课程")
    return p


def format_report(r: IngestReport) -> str:
    lines = [f"导入完成：{r.name}（{r.seconds:.1f}s）  课程 id {r.course_id}"]
    files = "、".join(f"{KIND_NAMES.get(k, k)} {n}" for k, n in r.files.items()) or "0"
    lines.append(f"文件：{files}（本次解析 {r.parsed}，复用 {r.reused}，移除 {r.removed}，失败 {r.failed}）")
    lines.append(f"章节（{len(r.chapters)}）：")
    for c in r.chapters:
        lines.append(f"  {c.n:2d}. {c.title}  [{', '.join(c.sources)}；{c.questions} 题]")
    lines.append(f"题目：{r.questions}（{r.answered} 题有参考答案）")
    scan = f"；扫描页 {r.scan_pages}" if r.scan_pages else ""
    lines.append(f"配图：{r.figures} 张 + 题目裁图 {r.crops} 张{scan}")
    lines.append(f"证据块：{r.evidence}")
    notes = list(r.warnings)
    for hit in r.injection:
        pages = ", ".join(str(p) for p in hit.pages[:8]) + ("…" if len(hit.pages) > 8 else "")
        notes.append(f"{hit.file} 第 {pages} 页有疑似对 AI 下指令的文字，讲解时会当作普通资料处理")
    if notes:
        lines.append("提醒：")
        lines.extend(f"  - {n}" for n in notes)
    return "\n".join(lines)


async def _ingest(args: argparse.Namespace, data_dir: Path, out: TextIO) -> int:
    db = Database.in_dir(data_dir)

    def progress(ev: IngestEvent) -> None:
        if args.quiet or args.json:
            return
        if isinstance(ev, FileDone):
            out.write(f"  ✓ {ev.rel}（{ev.pages} 页，{ev.figures} 图，{ev.seconds:.1f}s）\n")
        elif isinstance(ev, FileFailed):
            out.write(f"  ✗ {ev.rel}：{ev.error}\n")

    try:
        report = await ingest(
            db,
            data_dir,
            args.materials,
            name=args.name,
            lang=args.lang,
            workers=args.workers,
            on_event=progress,
        )
    except (FileNotFoundError, IngestBusyError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    finally:
        await db.close()
    out.write((report.model_dump_json(indent=2) if args.json else format_report(report)) + "\n")
    return 0


async def _search(args: argparse.Namespace, data_dir: Path, out: TextIO) -> int:
    db = Database.in_dir(data_dir)
    try:
        course = await find_course(db, args.course)
        index = await load_evidence(db, course.id)
    except CourseNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    finally:
        await db.close()
    query = " ".join(args.query)
    hits = index.search(query, args.k, chapter=args.chapter)
    if args.json:
        payload = [
            {
                "id": h.chunk.id,
                "file": h.chunk.rel,
                "page": h.chunk.page,
                "chapter": h.chunk.chapter,
                "score": h.score,
                "text": h.chunk.text,
            }
            for h in hits
        ]
        out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return 0 if hits else 4
    if not hits:
        out.write(f"资料里没有找到与“{query}”相关的内容。\n")
        return 4
    for h in hits:
        c = h.chunk
        out.write(f"[{c.id}] {c.rel} p.{c.page}（第 {c.chapter} 章，得分 {h.score:.2f}）\n")
        out.write(guard.fence(shorten(c.text, 300)) + "\n")
    return 0


async def _courses(data_dir: Path, out: TextIO) -> int:
    db = Database.in_dir(data_dir)
    try:
        rows = await list_courses(db)
    finally:
        await db.close()
    if not rows:
        out.write("还没有导入任何课程。运行：examprep ingest <资料目录>\n")
        return 0
    for c in rows:
        out.write(f"{c.id[:8]}  {c.name}  [{c.language or '-'}]  {c.root}\n")
    return 0


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    stream = out or sys.stdout
    reconfigure = getattr(sys.stdout, "reconfigure", None) if out is None else None
    if callable(reconfigure):
        reconfigure(errors="replace")  # Windows 控制台编码不支持的字符替换掉，不崩溃
    args = _parser().parse_args(list(argv) if argv is not None else None)
    data_dir: Path = args.data_dir or default_data_dir()
    if args.command == "ingest":
        return asyncio.run(_ingest(args, data_dir, stream))
    if args.command == "search":
        return asyncio.run(_search(args, data_dir, stream))
    return asyncio.run(_courses(data_dir, stream))
