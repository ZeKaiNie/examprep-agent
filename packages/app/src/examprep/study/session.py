"""学习会话（R3、R5、R6、R7.1）：意图 → 状态迁移 → 调用 Tutor / Grader / Verifier → 事件 → 落库。

流程全部由代码决定，模型只生成讲解、检查问题、讲评、提示、回答和小结。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from examprep_llm import (
    AssistantMessage,
    CancelToken,
    LLMError,
    LLMRequest,
    Message,
    TextBlock,
    TextDelta,
    UserMessage,
    estimate_tokens,
)
from examprep_runtime import LLMClient
from examprep_runtime.session import MESSAGE_EVENT, EventStore, StoredEvent, message_payload, project_messages

from ..agents import prompts
from ..agents.citations import CitationMap, CitationStream, has_markers, render
from ..agents.grader import grade_answer_llm, judge_check
from ..agents.verifier import Verification, verify
from ..catalog import load_evidence
from ..ingest import guard
from ..ingest.grading import grade_answer
from ..knowledge.evidence import EvidenceChunk, EvidenceIndex
from ..learner.records import AttemptInput, LearnerRecords
from ..pedagogy.intent import Awaiting, Intent, classify
from ..pedagogy.parts import QUIZ_N, part_chars, pick_profile, split_parts
from ..pedagogy.picker import Candidate, pick_questions
from ..pedagogy.strategy import Strategy, days_left, strategy_for
from ..store import Database
from ..store import models as m
from .context import LearnerSnapshot, learner_state, materials, system_messages, user_turn
from .events import (
    ChapterDone,
    ChapterRow,
    CitationRef,
    CourseDone,
    FigureRef,
    FiguresShown,
    Graded,
    NoteSaved,
    Notice,
    ProgressReport,
    QuestionShown,
    Refused,
    State,
    StateChanged,
    StudyEvent,
    Suggestions,
    TutorDelta,
    TutorKind,
    TutorMessage,
    Verified,
)

SEGMENT_TOKENS = 60_000  # 一段对话（同一章）的历史上限；超过后开新段，代价是一次缓存未命中
SEGMENT_EVENT = "segment"  # 会话事件：开始新的一段对话 {"chapter": n}
EVIDENCE_EVENT = "evidence"  # 会话事件：本段新编号的证据 {"ids": [...]}，恢复时按顺序重建编号表
_STATES: tuple[State, ...] = ("TEACH", "CHECK", "PRACTICE", "SELF", "DONE")
MAX_RETEACH = 2
ASK_HITS = 6
EMPTY_RETRIES = 1  # Tutor 返回空正文时的重试次数

_SELF_RIGHT = re.compile(r"^\s*(?:对|对了|我对了|答对了|正确|是对的|right|correct|yes|y)\W*$", re.I)
_SELF_WRONG = re.compile(r"^\s*(?:错|错了|我错了|答错了|不对|wrong|incorrect|no|n)\W*$", re.I)


# ---------------------------------------------------------------- 课程数据


@dataclass(slots=True)
class CourseData:
    id: str
    name: str
    language: str
    chapters: list[tuple[int, str]]
    chunks: dict[int, list[EvidenceChunk]]
    index: EvidenceIndex
    questions: dict[str, m.Question]
    page_figures: dict[tuple[str, int], list[FigureRef]]
    crops: dict[tuple[str, str], list[FigureRef]]  # (qid, question/answer)

    def title(self, n: int) -> str:
        return next((t for k, t in self.chapters if k == n), "")

    def next_chapter(self, n: int) -> int | None:
        later = [k for k, _ in self.chapters if k > n]
        return min(later) if later else None


async def load_course(db: Database, course_id: str) -> CourseData:
    async with db.session() as s:
        course = await s.get(m.Course, course_id)
        if course is None:
            raise LookupError(f"课程不存在：{course_id}")
        chapters = (
            await s.scalars(select(m.Chapter).where(m.Chapter.course_id == course_id).order_by(m.Chapter.n))
        ).all()
        questions = (
            await s.scalars(
                select(m.Question).where(m.Question.course_id == course_id).order_by(m.Question.qid)
            )
        ).all()
        figures = (
            await s.scalars(select(m.Figure).where(m.Figure.course_id == course_id).order_by(m.Figure.id))
        ).all()
    index = await load_evidence(db, course_id)
    by_chapter: dict[int, list[EvidenceChunk]] = defaultdict(list)
    for c in index.chunks:
        by_chapter[c.chapter].append(c)
    page_figs: dict[tuple[str, int], list[FigureRef]] = defaultdict(list)
    crops: dict[tuple[str, str], list[FigureRef]] = defaultdict(list)
    for f in figures:
        ref = FigureRef(file=f.rel_path, page=f.page, path=f.path)
        if f.kind == "figure":
            page_figs[(f.rel_path, f.page)].append(ref)
        elif f.question_id:
            crops[(f.question_id, f.kind)].append(ref)
    return CourseData(
        id=course.id,
        name=course.name,
        language=course.language or "zh",
        chapters=[(c.n, c.title) for c in chapters],
        chunks=dict(by_chapter),
        index=index,
        questions={q.qid: q for q in questions},
        page_figures=dict(page_figs),
        crops=dict(crops),
    )


# ---------------------------------------------------------------- 会话


@dataclass(slots=True)
class StudyOptions:
    mode: str | None = None  # cram / longterm
    level: str | None = None  # zero / weak / gap
    exam_date: str | None = None
    profile: str | None = None  # small / standard / large


@dataclass(slots=True)
class _Turn:
    """一次 Tutor 调用的结果。"""

    raw: str = ""
    text: str = ""
    ok: bool = False
    citations: list[CitationRef] = field(default_factory=list[CitationRef])


class StudySession:
    def __init__(
        self,
        db: Database,
        llm: LLMClient,
        course: CourseData,
        enrollment: m.Enrollment,
        progress: dict[int, m.ChapterProgress],
        events: EventStore,
        history: list[Message],
        *,
        context_window: int = 128_000,
        verify_answers: bool = True,
        timeout: float = 30.0,
        segment_chapter: int | None = None,
        cmap: CitationMap | None = None,
    ) -> None:
        self.db = db
        self.llm = llm
        self.course = course
        self.enr = enrollment
        self.progress = progress
        self.events = events
        # 当前这段对话（同一章，只追加）：历史、所属章、段内共用的证据编号表
        self.history = history
        self.segment_chapter = segment_chapter
        self.cmap = cmap or CitationMap()
        self.records = LearnerRecords(db, tenant=enrollment.tenant_id, user=enrollment.user_id)
        self.context_window = context_window
        self.verify_answers = verify_answers
        self.timeout = timeout
        self.cancel = CancelToken()
        profile = enrollment.profile or pick_profile(context_window)
        self.profile = profile
        target = part_chars(profile, course.language)
        self.parts = {n: split_parts(course.chunks.get(n, []), target) for n, _ in course.chapters}
        self.system_budget = max(2_000, int(context_window * 0.15))
        self.evidence_budget = max(2_000, min(int(context_window * 0.4), 20_000))

    # ------------------------------------------------------------ 打开与恢复

    @classmethod
    async def open(
        cls,
        db: Database,
        llm: LLMClient,
        course_id: str,
        *,
        events: EventStore,
        options: StudyOptions | None = None,
        context_window: int = 128_000,
        verify_answers: bool = True,
        timeout: float = 30.0,
    ) -> StudySession:
        await db.create_all()
        course = await load_course(db, course_id)
        records = LearnerRecords(db)
        enr = await records.enrollment(course_id)
        opts = options or StudyOptions()
        enr.mode = opts.mode or enr.mode
        enr.start_level = opts.level or enr.start_level
        enr.exam_date = opts.exam_date or enr.exam_date
        enr.profile = opts.profile or enr.profile
        numbers = [n for n, _ in course.chapters]
        if numbers and enr.current_chapter not in numbers:
            enr.current_chapter = numbers[0]
        history: list[Message] = []
        segment: int | None = None
        cmap = CitationMap()
        if enr.session_id is None or await events.get_session(enr.session_id) is None:
            info = await events.create_session(metadata={"course_id": course_id, "enrollment_id": enr.id})
            enr.session_id = info.id
        else:
            segment, history, cmap = restore_segment(await events.events(enr.session_id), course)
        await records.save_enrollment(enr)
        progress = await records.progress(enr.id, numbers)
        session = cls(
            db,
            llm,
            course,
            enr,
            progress,
            events,
            history,
            context_window=context_window,
            verify_answers=verify_answers,
            timeout=timeout,
            segment_chapter=segment,
            cmap=cmap,
        )
        for n, parts in session.parts.items():
            row = progress[n]
            if row.parts_total != len(parts):
                row.parts_total = len(parts)
                row.parts_done = min(row.parts_done, len(parts))
                await records.save_progress(row)
        return session

    # ------------------------------------------------------------ 状态

    @property
    def st(self) -> dict[str, object]:
        return self.enr.session_state

    @property
    def state(self) -> State:
        value = self.st.get("state", "TEACH")
        for s in _STATES:
            if value == s:
                return s
        return "TEACH"

    @property
    def chapter(self) -> int:
        return self.enr.current_chapter

    @property
    def strategy(self) -> Strategy:
        return strategy_for(self.enr.start_level, self.enr.mode, self.enr.exam_date)

    def awaiting(self) -> Awaiting:
        if self.state == "PRACTICE" and self.st.get("pending"):
            return "answer"
        if self.state == "CHECK":
            return "check"
        return None

    def _set_state(self, state: State) -> StateChanged:
        self.st["state"] = state
        return StateChanged(state=state, chapter=self.chapter)

    def _str(self, key: str) -> str | None:
        v = self.st.get(key)
        return v if isinstance(v, str) else None

    def _int(self, key: str, default: int = 0) -> int:
        v = self.st.get(key)
        return v if isinstance(v, int) else default

    def _queue(self) -> list[str]:
        v = self.st.get("queue")
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []  # pyright: ignore[reportUnknownVariableType]

    async def _save(self) -> None:
        await self.records.save_enrollment(self.enr)
        for row in self.progress.values():
            await self.records.save_progress(row)

    # ------------------------------------------------------------ 入口

    async def start(self) -> AsyncIterator[StudyEvent]:
        """打开会话时的欢迎信息：进度与建议。"""
        yield await self._progress_report()
        if self.state == "PRACTICE" and self.st.get("pending"):
            q = self.course.questions.get(str(self.st["pending"]))
            if q is not None:
                yield self._question_event(q)
        yield self._suggestions()

    async def handle(self, text: str) -> AsyncIterator[StudyEvent]:
        if self.state == "SELF":
            async for ev in self._self_assess(text):
                yield ev
        else:
            intent = classify(text, awaiting=self.awaiting())
            async for ev in self._dispatch(intent):
                yield ev
        await self._save()
        yield self._suggestions()

    async def _dispatch(self, intent: Intent) -> AsyncIterator[StudyEvent]:
        kind = intent.kind
        if kind == "progress":
            yield await self._progress_report()
        elif kind == "goto":
            async for ev in self._goto(intent.chapter or self.chapter):
                yield ev
        elif kind == "ask":
            async for ev in self._ask(intent.text):
                yield ev
        elif kind == "quiz":
            async for ev in self._start_practice(explicit=True):
                yield ev
        elif kind == "done":
            async for ev in self._wrapup():
                yield ev
        elif kind == "confused":
            async for ev in self._confused(intent.text):
                yield ev
        elif kind == "hint":
            async for ev in self._hint():
                yield ev
        elif kind in ("answer", "reveal", "skip"):
            async for ev in self._answer(intent):
                yield ev
        elif kind == "check_answer":
            async for ev in self._check_answer(intent.text):
                yield ev
        else:  # continue
            async for ev in self._continue():
                yield ev

    # ------------------------------------------------------------ 讲解

    async def _continue(self) -> AsyncIterator[StudyEvent]:
        if self.state == "DONE":
            yield Notice(
                text=self._t(
                    "这门课已经全部学完了。可以说“进度”查看，或“跳到第 N 章”复习。", "The course is finished."
                )
            )
            return
        if self.state == "CHECK":
            self.st.pop("check_q", None)  # 不回答检查题，直接往下讲
        async for ev in self._advance():
            yield ev

    async def _advance(self) -> AsyncIterator[StudyEvent]:
        n = self.chapter
        row = self.progress[n]
        parts = self.parts.get(n, [])
        has_questions = bool(self._chapter_questions(n))
        if self.strategy.practice_first and row.status == "todo" and row.practiced == 0 and has_questions:
            yield Notice(
                text=self._t(
                    "先做几道题摸摸底，错了再讲。", "A few questions first; we will teach what you miss."
                )
            )
            self.st["practice_first"] = True
            async for ev in self._start_practice():
                yield ev
            return
        if row.parts_done < len(parts):
            async for ev in self._teach(row.parts_done):
                yield ev
            row.parts_done += 1
            row.status = "teaching" if row.status in ("todo", "teaching") else row.status
            row.check_failures = 0
            self.st["last_part"] = row.parts_done - 1
            every = self.strategy.check_every
            if every and row.parts_done % every == 0:
                async for ev in self._ask_check(row.parts_done - 1):
                    yield ev
            else:
                yield self._set_state("TEACH")
            return
        target = self.strategy.questions_per_chapter
        if has_questions and row.practiced < target:
            async for ev in self._start_practice():
                yield ev
            return
        async for ev in self._wrapup():
            yield ev

    async def _teach(self, k: int, *, retry: int = 0) -> AsyncIterator[StudyEvent]:
        n = self.chapter
        parts = self.parts.get(n, [])
        if not parts:
            return
        chunks = parts[k]
        figs = self._figures_for(chunks)
        if figs:
            yield FiguresShown(figures=figs)
        task = prompts.teach(self.course.language, n, self.course.title(n), k + 1, len(parts), retry=retry)
        extra = ""
        if figs:
            listed = "、".join(f"{f.file} p.{f.page}" for f in figs)
            extra = self._t(
                f"本段配图已展示给学生：{listed}", f"Figures already shown to the student: {listed}"
            )
        student = self._t("继续", "continue") if not retry else self._t("这里没懂", "I don't get it")
        async for ev in self._tutor("reteach" if retry else "teach", task, chunks, student, extra=extra):
            yield ev

    async def _ask_check(self, k: int) -> AsyncIterator[StudyEvent]:
        turn = _Turn()
        async for ev in self._tutor(
            "check",
            prompts.check_question(self.course.language),
            self.parts[self.chapter][k],
            "",
            verify=False,
            out=turn,
        ):
            yield ev
        if turn.ok and turn.text.strip():
            self.st["check_q"] = turn.text
            self.st["check_part"] = k
            yield self._set_state("CHECK")
        else:
            yield self._set_state("TEACH")

    async def _check_answer(self, text: str) -> AsyncIterator[StudyEvent]:
        k = self._int("check_part", self._int("last_part"))
        question = self._str("check_q") or ""
        passage = (
            "\n\n".join(c.text for c in self.parts[self.chapter][k]) if self.parts.get(self.chapter) else ""
        )
        res = await judge_check(
            self.llm,
            passage=passage,
            question=question,
            answer=text,
            language=self.course.language,
            timeout=self.timeout,
        )
        verdict = (
            res.value.verdict if res.ok and res.value is not None else "right"
        )  # 判不了就不拦着学生往下学
        await self.records.record(
            self.enr.id,
            AttemptInput(
                kind="check",
                qid=None,
                chapter_n=self.chapter,
                answer=text,
                verdict=verdict if res.ok else "unsure",
                grader="llm" if res.ok else "self",
                score=res.value.score if res.value else None,
                reason=res.value.reason if res.value else res.error,
            ),
        )
        self.st.pop("check_q", None)
        if verdict == "wrong":
            yield Notice(
                text=self._t("看起来这一段还没完全懂，我换个讲法。", "Let me explain that part another way.")
            )
            async for ev in self._reteach_or_move_on(k, text):
                yield ev
            return
        if res.value is not None and res.value.reason:
            yield Notice(text=res.value.reason)
        yield self._set_state("TEACH")

    async def _confused(self, text: str) -> AsyncIterator[StudyEvent]:
        if self.state in ("PRACTICE", "SELF") and self.st.get("pending"):
            async for ev in self._hint():
                yield ev
            return
        k = self._int("last_part", -1)
        if k < 0:
            yield Notice(text=self._t("还没开始讲。说“继续”开始。", "We have not started yet. Say continue."))
            return
        self.st.pop("check_q", None)
        async for ev in self._reteach_or_move_on(k, text):
            yield ev

    async def _reteach_or_move_on(self, k: int, text: str) -> AsyncIterator[StudyEvent]:
        row = self.progress[self.chapter]
        row.check_failures += 1
        if row.check_failures == 1:
            note = self._t(f"第 {k + 1} 段：{text}", f"Part {k + 1}: {text}")
            await self.records.add_note(self.enr.id, "confusion", note, self.chapter)
            yield NoteSaved(note_type="confusion", chapter=self.chapter, text=note)
        if row.check_failures <= MAX_RETEACH:
            async for ev in self._teach(k, retry=row.check_failures):
                yield ev
            yield self._set_state("TEACH")
            return
        row.check_failures = 0
        yield Notice(
            text=self._t(
                "这一段先记为困惑点，复习时再回来。我们继续往下。",
                "Marked as confusing; we will come back to it.",
            )
        )
        yield self._set_state("TEACH")

    # ------------------------------------------------------------ 练习

    def _chapter_questions(self, n: int) -> list[m.Question]:
        return [q for q in self.course.questions.values() if q.chapter_n == n]

    async def _start_practice(self, *, explicit: bool = False) -> AsyncIterator[StudyEvent]:
        n = self.chapter
        pool = [Candidate(q.qid, q.chapter_n, q.is_exam) for q in self._chapter_questions(n)]
        if not pool:
            yield Notice(
                text=self._t("本章资料里没有题目。", "This chapter has no questions in the materials.")
            )
            if not explicit:
                async for ev in self._wrapup():
                    yield ev
            return
        row = self.progress[n]
        count = (
            QUIZ_N.get(self.profile, 3)
            if explicit
            else max(1, self.strategy.questions_per_chapter - row.practiced)
        )
        picked = pick_questions(
            pool,
            chapter=n,
            last_verdicts=await self.records.last_verdicts(self.enr.id),
            open_mistakes=await self.records.open_mistakes(self.enr.id),
            count=count,
        )
        self.st["queue"] = picked
        self.st["round_total"] = len(picked)
        self.st["round_wrong"] = 0
        async for ev in self._next_question():
            yield ev

    async def _next_question(self) -> AsyncIterator[StudyEvent]:
        queue = self._queue()
        if not queue:
            self.st.pop("pending", None)
            if self.st.pop("practice_first", None) and self._int("round_wrong") > 0:
                yield Notice(
                    text=self._t(
                        "有题没做对，我们从头讲这一章。",
                        "Some answers were wrong; let us go through the chapter.",
                    )
                )
                row = self.progress[self.chapter]
                row.status = "teaching"
                yield self._set_state("TEACH")
                async for ev in self._advance():
                    yield ev
                return
            async for ev in self._wrapup():
                yield ev
            return
        qid = queue[0]
        self.st["pending"] = qid
        self.st["hint"] = 0
        q = self.course.questions[qid]
        yield self._set_state("PRACTICE")
        yield self._question_event(q)

    def _question_event(self, q: m.Question) -> QuestionShown:
        total = self._int("round_total", 1)
        index = total - len(self._queue()) + 1
        return QuestionShown(
            qid=q.qid,
            chapter=q.chapter_n,
            qtype=q.type,
            stem=q.stem,
            options=list(q.options),
            points=q.points,
            source_file=str(q.source.get("file", "")),
            source_page=int(q.source.get("page", 0)),
            figures=self.course.crops.get((q.qid, "question"), []),
            has_answer=q.answer is not None,
            index=max(1, index),
            total=max(1, total),
        )

    def _pending(self) -> m.Question | None:
        qid = self._str("pending")
        return self.course.questions.get(qid) if qid else None

    async def _hint(self) -> AsyncIterator[StudyEvent]:
        q = self._pending()
        if q is None or self.state != "PRACTICE":
            yield Notice(
                text=self._t(
                    "现在没有待作答的题。说“考考我”开始练习。", "No question is waiting. Say quiz me."
                )
            )
            return
        level = self._int("hint") + 1
        if level > 3:
            async for ev in self._answer(Intent("reveal", "")):
                yield ev
            return
        self.st["hint"] = level
        task = prompts.hint(self.course.language, level)
        extra = self._question_block(q, with_reference=True, hide_reference=True)
        async for ev in self._tutor("hint", task, self._related(q), self._t("给个提示", "hint"), extra=extra):
            yield ev

    async def _answer(self, intent: Intent) -> AsyncIterator[StudyEvent]:
        q = self._pending()
        if q is None:
            yield Notice(text=self._t("现在没有待作答的题。", "No question is waiting."))
            return
        hints = self._int("hint")
        reference_src = self._ref_source(q)
        if intent.kind in ("reveal", "skip"):
            verdict = "skip"
            # “看答案”“跳过”是指令，不是答案：作答记录和讲评里都按未作答处理
            await self._record(q, "", verdict, "self", hints=hints)
            yield self._graded(q, verdict, None, "self", None, None, reveal=intent.kind == "reveal")
            if intent.kind == "reveal":
                async for ev in self._feedback(q, "", verdict, hints, reference_shown=True):
                    yield ev
            self._finish_question(wrong=True)
            async for ev in self._next_question():
                yield ev
            return
        rule, _shown = grade_answer(q.answer, q.type, list(q.options), intent.text)
        score: float | None = None
        reason: str | None = None
        error_type: str | None = None
        if rule in ("right", "wrong"):
            verdict, grader = rule, "rule"
            score = 1.0 if rule == "right" else 0.0
        elif q.answer:
            res = await grade_answer_llm(
                self.llm,
                question=q.stem,
                options=list(q.options),
                reference=q.answer,
                answer=intent.text,
                language=self.course.language,
                timeout=self.timeout,
            )
            if res.ok and res.value is not None:
                verdict, grader = res.value.verdict, "llm"
                score, reason, error_type = res.value.score, res.value.reason, res.value.error_type
            else:
                # Grader 失败：展示参考答案，请学生自评
                self.st["self_answer"] = intent.text
                yield self._graded(q, "unsure", None, "self", res.error, None, reveal=True)
                yield Notice(
                    text=self._t(
                        "自动判分没有完成。请对照参考答案，回复“对了”或“错了”。",
                        'Please compare with the reference and reply "right" or "wrong".',
                    )
                )
                yield self._set_state("SELF")
                return
        else:
            verdict, grader = "unsure", "self"
            reason = self._t(
                "这道题资料里没有参考答案，下面的讲评是 AI 的判断，不计入掌握度。",
                "No reference answer in the materials.",
            )
        await self._record(
            q, intent.text, verdict, grader, hints=hints, score=score, reason=reason, error_type=error_type
        )
        yield self._graded(
            q, verdict, score, grader, reason, error_type, reveal=verdict != "right" or bool(reference_src)
        )
        async for ev in self._feedback(q, intent.text, verdict, hints, reference_shown=verdict != "right"):
            yield ev
        self._finish_question(wrong=verdict in ("wrong", "partial"))
        async for ev in self._next_question():
            yield ev

    async def _self_assess(self, text: str) -> AsyncIterator[StudyEvent]:
        q = self._pending()
        if q is None:
            yield self._set_state("TEACH")
            return
        if _SELF_RIGHT.match(text):
            verdict = "right"
        elif _SELF_WRONG.match(text):
            verdict = "wrong"
        else:
            yield Notice(text=self._t("请回复“对了”或“错了”。", 'Please reply "right" or "wrong".'))
            return
        answer = self._str("self_answer") or ""
        await self._record(q, answer, verdict, "self", hints=self._int("hint"))
        self.st.pop("self_answer", None)
        yield self._graded(q, verdict, None, "self", None, None, reveal=False)
        self._finish_question(wrong=verdict == "wrong")
        async for ev in self._next_question():
            yield ev

    def _finish_question(self, *, wrong: bool) -> None:
        queue = self._queue()
        if queue:
            queue.pop(0)
        self.st["queue"] = queue
        self.st["hint"] = 0
        self.progress[self.chapter].practiced += 1
        if wrong:
            self.st["round_wrong"] = self._int("round_wrong") + 1

    async def _record(
        self,
        q: m.Question,
        answer: str,
        verdict: str,
        grader: str,
        *,
        hints: int,
        score: float | None = None,
        reason: str | None = None,
        error_type: str | None = None,
    ) -> None:
        await self.records.record(
            self.enr.id,
            AttemptInput(
                kind="question",
                qid=q.qid,
                chapter_n=q.chapter_n,
                answer=answer,
                verdict=verdict,
                grader=grader,
                score=score,
                reason=reason,
                error_type=error_type,
                hints_used=hints,
                has_reference=q.answer is not None,
            ),
        )

    def _graded(
        self,
        q: m.Question,
        verdict: str,
        score: float | None,
        grader: str,
        reason: str | None,
        error_type: str | None,
        *,
        reveal: bool,
    ) -> Graded:
        return Graded(
            qid=q.qid,
            verdict=verdict,
            score=score,
            grader=grader,
            reason=reason,
            error_type=error_type,
            reference=q.answer if reveal else None,
            reference_source=self._ref_source(q) if reveal else None,
            answer_figures=self.course.crops.get((q.qid, "answer"), []) if reveal else [],
        )

    @staticmethod
    def _ref_source(q: m.Question) -> str | None:
        src = q.answer_source
        if not src or not q.answer:
            return None
        return f"{src.get('file')} p.{src.get('page')}"

    async def _feedback(
        self, q: m.Question, answer: str, verdict: str, hints: int, *, reference_shown: bool
    ) -> AsyncIterator[StudyEvent]:
        task = prompts.feedback(
            self.course.language, verdict, hints_used=hints, reference_shown=reference_shown
        )
        extra = (
            self._question_block(q, with_reference=True)
            + "\n\n"
            + self._t("学生答案：", "Student answer: ")
            + guard.fence(answer or "（未作答）")
        )
        async for ev in self._tutor(
            "feedback", task, self._related(q), answer or self._t("看答案", "show answer"), extra=extra
        ):
            yield ev

    def _question_block(self, q: m.Question, *, with_reference: bool, hide_reference: bool = False) -> str:
        zh = self.course.language == "zh"
        stem = q.stem + ("\n" + "\n".join(q.options) if q.options else "")
        out = [("## 题目\n" if zh else "## Question\n") + guard.fence(stem)]
        if with_reference and q.answer:
            title = (
                "## 参考答案（不要直接告诉学生）\n"
                if hide_reference and zh
                else "## 参考答案\n"
                if zh
                else "## Reference answer\n"
            )
            out.append(title + guard.fence(q.answer))
        elif with_reference:
            out.append(
                "（资料里没有参考答案；你的判断要以 🟡 开头）"
                if zh
                else "(No reference answer; start your judgement with 🟡)"
            )
        return "\n\n".join(out)

    def _related(self, q: m.Question) -> list[EvidenceChunk]:
        query = q.stem + " " + " ".join(q.options)
        hits = self.course.index.search(query, 4, chapter=q.chapter_n)
        return [h.chunk for h in hits]

    # ------------------------------------------------------------ 提问

    async def _ask(self, text: str) -> AsyncIterator[StudyEvent]:
        hits = self.course.index.search(text, ASK_HITS, chapter=self.chapter)
        if len(hits) < 2:
            seen = {h.chunk.id for h in hits}
            hits += [h for h in self.course.index.search(text, ASK_HITS) if h.chunk.id not in seen]
        if not hits:
            msg = prompts.refusal(self.course.language)
            yield Refused(query=text, text=msg)
            await self._ensure_segment()
            await self._log(
                UserMessage(content=[TextBlock(text=text)]), AssistantMessage(content=[TextBlock(text=msg)])
            )
            return
        task = prompts.answer_question(self.course.language, text)
        async for ev in self._tutor("answer", task, [h.chunk for h in hits[:ASK_HITS]], text):
            yield ev

    # ------------------------------------------------------------ 小结与换章

    async def _wrapup(self) -> AsyncIterator[StudyEvent]:
        n = self.chapter
        row = self.progress[n]
        self.st.pop("pending", None)
        self.st["queue"] = []
        turn = _Turn()
        chunks = self.course.chunks.get(n, [])
        if chunks:
            task = prompts.wrapup(self.course.language, n, self.course.title(n))
            async for ev in self._tutor("wrapup", task, chunks, self._t("这章好了", "done"), out=turn):
                yield ev
            if turn.ok and turn.text.strip():
                await self.records.add_note(self.enr.id, "summary", turn.text, n)
                yield NoteSaved(note_type="summary", chapter=n, text=turn.text)
        verified = await self.records.chapter_verified(self.enr.id, n)
        row.status = "verified" if verified else "covered"
        row.parts_done = row.parts_total
        yield ChapterDone(chapter=n, status=row.status)
        nxt = self.course.next_chapter(n)
        if nxt is None:
            yield self._set_state("DONE")
            yield CourseDone()
            return
        self.enr.current_chapter = nxt
        self.st.pop("last_part", None)
        yield self._set_state("TEACH")

    async def _goto(self, n: int) -> AsyncIterator[StudyEvent]:
        if n not in self.progress:
            yield Notice(level="warning", text=self._t(f"没有第 {n} 章。", f"There is no chapter {n}."))
            return
        self.enr.current_chapter = n
        for key in ("pending", "check_q", "last_part", "practice_first"):
            self.st.pop(key, None)
        self.st["queue"] = []
        row = self.progress[n]
        if row.parts_done >= row.parts_total and row.status in ("covered", "verified"):
            row.parts_done = 0  # 复习已学完的章：从头讲
        yield self._set_state("TEACH")
        async for ev in self._advance():
            yield ev

    # ------------------------------------------------------------ 进度与建议

    async def _progress_report(self) -> ProgressReport:
        verdicts = await self.records.last_verdicts(self.enr.id)
        rows: list[ChapterRow] = []
        for n, title in self.course.chapters:
            qs = self._chapter_questions(n)
            p = self.progress[n]
            rows.append(
                ChapterRow(
                    n=n,
                    title=title,
                    status=p.status,
                    parts_done=p.parts_done,
                    parts_total=p.parts_total,
                    questions=len(qs),
                    correct=sum(1 for q in qs if verdicts.get(q.qid) == "right"),
                )
            )
        return ProgressReport(
            course=self.course.name,
            current=self.chapter,
            mode=self.enr.mode,
            days_left=days_left(self.enr.exam_date),
            chapters=rows,
            open_mistakes=len(await self.records.open_mistakes(self.enr.id)),
        )

    def _suggestions(self) -> Suggestions:
        zh = self.course.language == "zh"
        state = self.state
        if state == "PRACTICE":
            items = ["给个提示", "看答案", "跳过"] if zh else ["hint", "show the answer", "skip"]
        elif state == "CHECK":
            items = (
                ["（直接回答上面的问题）", "没懂", "继续"]
                if zh
                else ["(answer the question above)", "I don't get it", "continue"]
            )
        elif state == "SELF":
            items = ["对了", "错了"] if zh else ["right", "wrong"]
        elif state == "DONE":
            items = ["进度", "跳到第 1 章"] if zh else ["progress", "go to chapter 1"]
        else:
            items = ["继续", "考考我", "这里没懂"] if zh else ["continue", "quiz me", "I don't get it"]
        return Suggestions(items=items)

    # ------------------------------------------------------------ Tutor 调用

    async def _tutor(
        self,
        kind: TutorKind,
        task: str,
        chunks: Sequence[EvidenceChunk],
        student: str,
        *,
        extra: str = "",
        verify: bool = True,
        out: _Turn | None = None,
    ) -> AsyncIterator[StudyEvent]:
        turn = out if out is not None else _Turn()
        lang = self.course.language
        await self._ensure_segment()
        cmap = self.cmap.copy()  # 本轮成功后才提交新编号
        mats = materials(chunks, cmap, budget=self.evidence_budget, language=lang)
        row = self.progress[self.chapter]
        snap = LearnerSnapshot(
            chapter=self.chapter,
            chapter_title=self.course.title(self.chapter),
            part=min(row.parts_done + 1, max(row.parts_total, 1)),
            parts=row.parts_total,
            status=row.status,
            confusions=[
                n.text
                for n in await self.records.notes(self.enr.id, type="confusion", chapter_n=self.chapter)
            ],
            open_mistakes=len(await self.records.open_mistakes(self.enr.id)),
        )
        system = system_messages(
            lang, self.course.name, self.course.chapters, chapter=self.chapter, budget=self.system_budget
        )
        user_text = user_turn(lang, student, task, mats.text, extra, state=learner_state(lang, snap))
        prompt = UserMessage(content=[TextBlock(text=user_text)])
        request = LLMRequest(messages=[*system, *self.history, prompt])
        stream = CitationStream(cmap)
        raw: list[str] = []
        shown: list[str] = []
        # 真实模型（deepseek-flash 思考模式）偶尔只输出推理、正文为空：空回复没有向学生输出任何内容，重试一次
        for _attempt in range(EMPTY_RETRIES + 1):
            stream = CitationStream(cmap)
            raw, shown = [], []
            try:
                async for ev in self.llm.stream(request, role="tutor", cancel=self.cancel):
                    if isinstance(ev, TextDelta):
                        raw.append(ev.text)
                        piece = stream.feed(ev.text)
                        if piece:
                            shown.append(piece)
                            yield TutorDelta(text=piece)
            except LLMError as exc:
                yield Notice(level="error", text=self._t(f"模型调用失败：{exc}", f"Model call failed: {exc}"))
                return
            if "".join(raw).strip():
                break
        else:
            yield Notice(
                level="error",
                text=self._t(
                    "模型这次没有给出内容，请再说一次。", "The model returned nothing; please try again."
                ),
            )
            return
        tail = stream.flush()
        if tail:
            shown.append(tail)
            yield TutorDelta(text=tail)
        raw_text = "".join(raw)
        rendered = stream.result("".join(shown))
        citations = [
            CitationRef(label=c.label, evidence_id=c.evidence_id, file=c.file, page=c.page)
            for c in rendered.citations
        ]
        yield TutorMessage(kind=kind, text=rendered.text, citations=citations, unknown=rendered.unknown)
        final_raw = raw_text
        if verify and self.verify_answers and has_markers(raw_text):
            v = await verify_text(self.llm, raw_text, cmap, lang, self.timeout)
            if v is not None:
                final_raw = v.text
                yield Verified(
                    total=v.total,
                    supported=v.supported,
                    partial=v.partial,
                    unsupported=v.unsupported,
                    text=render(final_raw, cmap).text,
                )
        turn.raw = final_raw
        turn.text = render(final_raw, cmap).text
        turn.ok = True
        turn.citations = citations
        # 历史里放发出去的原文和模型的原样输出（不放校验后加 🟡 的版本），下一轮请求才是这一轮的前缀扩展
        self.cmap = cmap
        if mats.new_ids:
            await self._event(EVIDENCE_EVENT, {"ids": mats.new_ids})
        await self._log(prompt, AssistantMessage(content=[TextBlock(text=raw_text)]))

    # ------------------------------------------------------------ 对话分段（缓存前缀）

    def _segment_limit(self) -> int:
        return min(int(self.context_window * 0.5), SEGMENT_TOKENS)

    async def _ensure_segment(self) -> None:
        """换章或本段太长时开新段：系统消息变化或历史太长，前缀反正保不住，从头开始只追加。"""
        if (
            self.segment_chapter == self.chapter
            and estimate_tokens(_history_text(self.history)) < self._segment_limit()
        ):
            return
        self.segment_chapter = self.chapter
        self.history = []
        self.cmap = CitationMap()
        await self._event(SEGMENT_EVENT, {"chapter": self.chapter})

    async def _event(self, type: str, payload: dict[str, Any]) -> None:
        if self.enr.session_id is not None:
            await self.events.append(self.enr.session_id, type, payload)

    async def _log(self, user: UserMessage | None, assistant: AssistantMessage) -> None:
        for msg in (user, assistant):
            if msg is None:
                continue
            self.history.append(msg)
            await self._event(MESSAGE_EVENT, message_payload(msg))

    def _figures_for(self, chunks: Sequence[EvidenceChunk]) -> list[FigureRef]:
        out: list[FigureRef] = []
        for c in chunks:
            for f in self.course.page_figures.get((c.rel, c.page), []):
                if f not in out:
                    out.append(f)
        return out

    def _t(self, zh: str, en: str) -> str:
        return zh if self.course.language == "zh" else en


def _history_text(history: Sequence[Message]) -> str:
    return "\n".join(m.text for m in history if isinstance(m, (UserMessage, AssistantMessage)))


def restore_segment(
    events: Sequence[StoredEvent], course: CourseData
) -> tuple[int | None, list[Message], CitationMap]:
    """从会话事件恢复最后一段对话：最后一个 segment 事件之后的消息与证据编号。

    没有 segment 事件（旧版本的会话）时返回空段，下一轮从新段开始。
    """
    start = max((i for i, e in enumerate(events) if e.type == SEGMENT_EVENT), default=None)
    if start is None:
        return None, [], CitationMap()
    chapter = events[start].payload.get("chapter")
    by_id = {c.id: c for c in course.index.chunks}
    cmap = CitationMap()
    tail = events[start + 1 :]
    for e in tail:
        if e.type == EVIDENCE_EVENT:
            ids = e.payload.get("ids")
            for eid in ids if isinstance(ids, list) else []:  # pyright: ignore[reportUnknownVariableType]
                chunk = by_id.get(str(eid))  # pyright: ignore[reportUnknownArgumentType]
                if chunk is None:
                    # 资料重新导入后证据变了：历史里的编号对不上，丢弃这段，下一轮开新段
                    return None, [], CitationMap()
                cmap.add(chunk)
    return (chapter if isinstance(chapter, int) else None), project_messages(tail), cmap


async def verify_text(
    llm: LLMClient, text: str, cmap: CitationMap, language: str, timeout: float
) -> Verification | None:
    """校验失败返回 None（不改动文本）。"""
    v = await verify(llm, text, cmap, language=language, timeout=timeout)
    return None if v.error is not None or v.total == 0 else v
