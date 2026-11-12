"""命令行入口：examprep ingest / search / courses / study。"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from examprep_llm import Gateway
from examprep_runtime.session import SQLiteEventStore

from . import __version__
from .catalog import CourseNotFoundError, find_course, list_courses, load_evidence
from .ingest import guard
from .ingest.events import FileDone, FileFailed, IngestEvent, IngestReport
from .ingest.pipeline import COURSES_DIR, ingest
from .ingest.text import shorten
from .learner.lease import Lease, LeaseHeldError
from .learner.records import LearnerRecords
from .settings import REASONING_LEVELS, SESSIONS_DB, SettingsError, load_compaction, load_settings
from .store import Database
from .store.jobs import IngestBusyError
from .study.console import run_console
from .study.session import StudyOptions, StudySession

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

    pt = sub.add_parser("study", help="开始或继续学习一门课（交互式）")
    pt.add_argument("course", help="课程名称、id 或资料目录")
    pt.add_argument("--mode", choices=["cram", "longterm"], default=None, help="突击 / 长期")
    pt.add_argument("--level", choices=["zero", "weak", "gap"], default=None, help="零基础 / 补弱 / 查缺")
    pt.add_argument("--exam-date", default=None, help="考试日期 YYYY-MM-DD")
    pt.add_argument(
        "--profile",
        choices=["small", "standard", "large"],
        default=None,
        help="每段讲多少（默认按模型自动选）",
    )
    pt.add_argument("--no-verify", action="store_true", help="不做逐句核对（更快、更省）")
    pt.add_argument(
        "--reasoning",
        choices=list(REASONING_LEVELS),
        default=None,
        help="讲解的思考强度（默认 high；关掉更快更省，但例子更容易算错）",
    )
    pt.add_argument(
        "--no-auto-compact",
        action="store_true",
        help="关闭自动压缩：对话太长时直接开新对话（学习时说“压缩上下文”仍可手动压缩）",
    )

    ps = sub.add_parser("serve", help="启动本机网页服务（默认只允许本机访问）")
    ps.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    ps.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
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


async def _study(
    args: argparse.Namespace,
    data_dir: Path,
    out: TextIO,
    read_line: Callable[[], str],
    gateway: Gateway | None,
) -> int:
    db = Database.in_dir(data_dir)
    store = SQLiteEventStore(data_dir / SESSIONS_DB)
    gw = gateway
    try:
        course = await find_course(db, args.course)
        if gw is None:
            gw = Gateway.from_config(load_settings(data_dir))
        if args.reasoning is not None:
            gw.reasoning["tutor"] = args.reasoning
        compaction = load_compaction(data_dir)
        if args.no_auto_compact:
            compaction = dataclasses.replace(compaction, auto=False)
        tutor = gw.chain(role="tutor")[0]
        # 同一门课同一时间只能在一个地方学（另一个命令行窗口或 examprep serve）
        async with Lease(db, (await LearnerRecords(db).enrollment(course.id)).id):
            session = await StudySession.open(
                db,
                gw,
                course.id,
                events=store,
                options=StudyOptions(
                    mode=args.mode, level=args.level, exam_date=args.exam_date, profile=args.profile
                ),
                context_window=gw.model_info(tutor).context_window,
                verify_answers=not args.no_verify,
                compaction=compaction,
            )
            level = gw.reasoning.get("tutor") or "厂商默认"
            auto = (
                f"对话超过约 {session.compact_limit() // 1000}k token 自动压缩"
                if compaction.auto
                else "自动压缩已关闭"
            )
            out.write(
                f"开始学习：{course.name}（讲解模型 {tutor}，思考强度 {level}；{auto}，说“压缩上下文”可随时压缩）。"
                "输入“退出”结束，进度会自动保存。\n"
            )
            return await run_console(session, out, read_line, data_dir / COURSES_DIR / course.id)
    except (CourseNotFoundError, SettingsError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    except LeaseHeldError as exc:
        sys.stderr.write(f"{exc}\n")
        return 3
    finally:
        await store.close()
        if gw is not None and gateway is None:
            await gw.aclose()
        await db.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    read_line: Callable[[], str] | None = None,
    gateway: Gateway | None = None,
) -> int:
    """``read_line`` 与 ``gateway`` 供测试注入（脚本化输入、假模型）。"""
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
    if args.command == "study":
        return asyncio.run(_study(args, data_dir, stream, read_line or sys.stdin.readline, gateway))
    if args.command == "serve":
        return _serve(args, data_dir, stream)
    return asyncio.run(_courses(data_dir, stream))


def _serve(args: argparse.Namespace, data_dir: Path, out: TextIO) -> int:
    import uvicorn

    from .server import create_app
    from .server.security import LOOPBACK

    host: str = args.host
    if host in ("0.0.0.0", "::", ""):
        sys.stderr.write(
            "请指定一个具体的地址（例如 127.0.0.1 或本机局域网 IP）；服务没有登录，不要对所有网卡开放。\n"
        )
        return 2
    if host not in LOOPBACK:
        out.write(f"注意：服务没有登录鉴权，{host} 上能访问到它的人都能用你的资料和模型额度。\n")
    shown = f"[{host}]" if ":" in host else host
    out.write(f"ExamPrep Agent 已启动：http://{shown}:{args.port}  （接口说明 /docs，Ctrl+C 停止）\n")
    out.flush()
    app = create_app(data_dir, extra_hosts=(host,))
    uvicorn.run(app, host=host, port=args.port, log_level="warning")
    return 0
