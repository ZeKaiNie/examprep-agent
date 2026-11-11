"""命令行里的学习界面：读一行输入，流式显示学习事件。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .events import (
    ChapterDone,
    CourseDone,
    FiguresShown,
    Graded,
    NoteSaved,
    Notice,
    ProgressReport,
    QuestionShown,
    Refused,
    StudyEvent,
    Suggestions,
    TutorDelta,
    TutorMessage,
    Verified,
)
from .session import StudySession

EXIT_WORDS = {"退出", "exit", "quit", "q", "再见", "bye"}
VERDICT_MARK = {
    "right": "✅ 答对了",
    "partial": "🟡 部分正确",
    "wrong": "❌ 答错了",
    "unsure": "🟡 无法自动判分",
    "skip": "⏭ 已跳过",
}
STATUS_NAME = {"todo": "未开始", "teaching": "学习中", "covered": "已讲完", "verified": "已验证"}
GRADER_NAME = {"rule": "规则判分", "llm": "AI 判分", "self": "自评"}


class Console:
    def __init__(self, out: TextIO, course_dir: Path) -> None:
        self.out = out
        self.course_dir = course_dir
        self._streaming = False

    def w(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def show(self, ev: StudyEvent) -> None:
        if isinstance(ev, TutorDelta):
            if not self._streaming:
                self.out.write("\n")
                self._streaming = True
            self.out.write(ev.text)
            self.out.flush()
            return
        if self._streaming:
            self.out.write("\n")
            self._streaming = False
        if isinstance(ev, TutorMessage):
            if ev.unknown:
                self.w(f"   （已删除 {len(ev.unknown)} 个资料中不存在的引用）")
        elif isinstance(ev, Verified):
            if ev.unsupported or ev.partial:
                self.w(
                    f"   核对：{ev.supported} 句有依据，{ev.partial} 句部分有依据，{ev.unsupported} 句在资料中找不到依据（已标 🟡）"
                )
                for line in ev.text.split("\n"):
                    if "🟡" in line:
                        self.w(f"   {line.strip()}")
            else:
                self.w(f"   核对：{ev.supported} 句均有资料依据")
        elif isinstance(ev, FiguresShown):
            for f in ev.figures:
                self.w(f"🖼 {f.file} p.{f.page}：{self.course_dir / f.path}")
        elif isinstance(ev, QuestionShown):
            pts = f"（{ev.points} 分）" if ev.points else ""
            self.w(
                f"\n📝 第 {ev.index}/{ev.total} 题 [{ev.qid}]{pts}  出处：{ev.source_file} p.{ev.source_page}"
            )
            self.w(ev.stem)
            for o in ev.options:
                self.w(f"  {o}")
            for f in ev.figures:
                self.w(f"🖼 题图：{self.course_dir / f.path}")
        elif isinstance(ev, Graded):
            self.w(f"\n{VERDICT_MARK.get(ev.verdict, ev.verdict)}（{GRADER_NAME.get(ev.grader, ev.grader)}）")
            if ev.reason:
                self.w(f"   {ev.reason}")
            if ev.reference:
                src = f"（{ev.reference_source}）" if ev.reference_source else ""
                self.w(f"参考答案{src}：\n{ev.reference}")
            for f in ev.answer_figures:
                self.w(f"🖼 答案图：{self.course_dir / f.path}")
        elif isinstance(ev, Refused):
            self.w(f"\n{ev.text}")
        elif isinstance(ev, NoteSaved):
            name = {"confusion": "困惑点", "summary": "本章小结"}.get(ev.note_type, "笔记")
            self.w(f"   （已记为{name}）")
        elif isinstance(ev, ChapterDone):
            self.w(f"\n🏁 第 {ev.chapter} 章完成：{STATUS_NAME.get(ev.status, ev.status)}")
        elif isinstance(ev, CourseDone):
            self.w("🎉 整门课学完了！")
        elif isinstance(ev, ProgressReport):
            left = f"，距考试 {ev.days_left} 天" if ev.days_left is not None else ""
            self.w(
                f"\n📚 {ev.course}（{'突击' if ev.mode == 'cram' else '长期'}模式{left}；待回炉错题 {ev.open_mistakes} 道）"
            )
            for c in ev.chapters:
                mark = "→" if c.n == ev.current else " "
                self.w(
                    f" {mark} {c.n}. {c.title}  {STATUS_NAME.get(c.status, c.status)}  "
                    f"段 {c.parts_done}/{c.parts_total}  题 {c.correct}/{c.questions}"
                )
        elif isinstance(ev, Notice):
            prefix = {"error": "⚠️ ", "warning": "⚠️ "}.get(ev.level, "")
            self.w(f"{prefix}{ev.text}")
        elif isinstance(ev, Suggestions):
            self.w("\n下一步：" + " | ".join(ev.items))


async def run_console(
    session: StudySession, out: TextIO, read_line: Callable[[], str], course_dir: Path
) -> int:
    ui = Console(out, course_dir)
    async for ev in session.start():
        ui.show(ev)
    while True:
        out.write("\n你> ")
        out.flush()
        line = await asyncio.to_thread(read_line)
        if line == "":  # EOF
            break
        text = line.strip()
        if text.lower() in EXIT_WORDS:
            break
        async for ev in session.handle(text):
            ui.show(ev)
    ui.w("\n进度已保存，下次运行 examprep study 接着学。")
    return 0
