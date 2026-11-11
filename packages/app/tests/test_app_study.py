"""学习会话端到端：假模型按请求内容回复（Tutor / Grader / Verifier），在中文样例上走完整流程。"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from conftest import SAMPLES

from examprep.ingest.pipeline import ingest
from examprep.learner.records import LearnerRecords
from examprep.store import Database
from examprep.study.events import (
    ChapterDone,
    CourseDone,
    Graded,
    NoteSaved,
    Notice,
    ProgressReport,
    QuestionShown,
    Refused,
    StateChanged,
    StudyEvent,
    Suggestions,
    TutorDelta,
    TutorMessage,
    Verified,
)
from examprep.study.session import StudyOptions, StudySession
from examprep_llm import Gateway, LLMError, LLMRequest, ServerError, SystemMessage, UserMessage
from examprep_llm.testing import FakeClock, FakeProvider, FakeTurn
from examprep_runtime.session import SQLiteEventStore


class Brain:
    """按请求内容生成回复，并记录每类调用的次数。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_grader_on: set[str] = set()

    def __call__(self, req: LLMRequest) -> FakeTurn | LLMError:
        system = "\n".join(m.content for m in req.messages if isinstance(m, SystemMessage))
        user = [m for m in req.messages if isinstance(m, UserMessage)][-1].text
        if any(t.name == "submit_result" for t in req.tools):
            if "核对员" in system:
                self.calls.append("verify")
                items = re.findall(r"^(\d+)\. (.*)$", user, re.M)
                verdicts = [
                    {"index": int(i), "verdict": "unsupported" if "编造" in s else "supported"}
                    for i, s in items
                ]
                return FakeTurn(tool_calls=[("submit_result", {"sentences": verdicts})])
            if "判分员" in system:
                self.calls.append("grade")
                answer = user.split("## 学生答案")[-1]
                if any(key in answer for key in self.fail_grader_on):
                    return ServerError("grader down")
                right = "LIFO" in answer
                return FakeTurn(
                    tool_calls=[
                        (
                            "submit_result",
                            {
                                "verdict": "right" if right else "wrong",
                                "score": 1 if right else 0,
                                "error_type": "none" if right else "concept",
                                "reason": "意思一致" if right else "概念错误",
                            },
                        )
                    ]
                )
            self.calls.append("judge")
            wrong = "不知道" in user.split("## 学生回答")[-1]
            return FakeTurn(
                tool_calls=[
                    (
                        "submit_result",
                        {
                            "verdict": "wrong" if wrong else "right",
                            "score": 0 if wrong else 1,
                            "reason": "好",
                        },
                    )
                ]
            )
        task = user.split("## 本轮任务")[-1]
        if "换一种讲法" in task:
            kind, text = "reteach", "换个讲法：线性表就像排队 [[E1]]。"
        elif "检查理解" in task:
            kind, text = "check", "顺序表插入为什么要移动元素？"
        elif "提示" in task:
            kind, text = "hint", "提示：想想插入位置之后有几个元素 [[E1]]。"
        elif "讲评" in task:
            kind, text = "feedback", "讲评：参考答案已给出 [[E1]]。"
        elif "学生提问" in task:
            kind, text = "answer", "循环队列判满看 (rear + 1) % MaxSize == front [[E1]]。"
        elif "小结" in task:
            kind, text = "wrapup", "- 要点一 [[E1]]\n- 要点二 [[E2]]"
        else:
            kind, text = (
                "teach",
                "线性表是有序序列 [[E1]]。这句是编造的 [[E1]]。这里引用了不存在的编号 [[E9]]。",
            )
        self.calls.append(kind)
        return FakeTurn(text=text, chunk_size=3)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    db = Database.in_dir(tmp_path / "data")
    report = await ingest(db, tmp_path / "data", SAMPLES / "zh-data-structures")
    brain = Brain()
    fake = FakeProvider(responder=brain)
    gw = Gateway({"fake": fake}, {r: ["fake/m"] for r in ("tutor", "grader", "verifier")}, clock=FakeClock())
    store = SQLiteEventStore(tmp_path / "data" / "sessions.db")
    yield {"db": db, "gw": gw, "brain": brain, "course": report.course_id, "store": store, "tmp": tmp_path}
    await store.close()
    await db.close()


async def open_session(env: dict[str, Any], **opts: Any) -> StudySession:
    return await StudySession.open(
        env["db"], env["gw"], env["course"], events=env["store"], options=StudyOptions(**opts)
    )


async def say(session: StudySession, text: str) -> list[StudyEvent]:
    return [ev async for ev in session.handle(text)]


def of(events: list[StudyEvent], cls: type[Any]) -> list[Any]:
    return [e for e in events if isinstance(e, cls)]


def last_state(events: list[StudyEvent]) -> str:
    return of(events, StateChanged)[-1].state


async def test_full_chapter_teach_check_practice_wrapup(env: dict[str, Any]) -> None:
    s = await open_session(env)
    start = [ev async for ev in s.start()]
    assert isinstance(start[0], ProgressReport)
    assert [c.title for c in start[0].chapters] == ["线性表", "栈和队列", "树与二叉树"]
    assert isinstance(start[-1], Suggestions)

    # 1. 讲第 1 段：引用由系统生成，编造的编号被删除，Verifier 标出不支撑的句子，随后出检查题
    ev = await say(s, "继续")
    teach = of(ev, TutorMessage)[0]
    assert teach.kind == "teach"
    assert "（第1章_线性表.md p.1）" in teach.text
    assert "[[" not in teach.text
    assert teach.unknown == ["E9"]
    assert teach.citations[0].file == "第1章_线性表.md"
    streamed = "".join(d.text for d in of(ev, TutorDelta))
    assert streamed.startswith(teach.text)  # 流式输出与最终渲染一致（检查题紧随其后）
    verified = of(ev, Verified)[0]
    assert (verified.total, verified.supported, verified.unsupported) == (3, 1, 2)
    assert "🟡 这句是编造的" in verified.text
    assert "🟡 这里引用了不存在的编号" in verified.text  # 只引用编造编号的句子直接标 🟡
    assert of(ev, TutorMessage)[1].kind == "check"
    assert last_state(ev) == "CHECK"
    assert of(ev, Suggestions)[-1].items[0] == "（直接回答上面的问题）"

    # 2. 检查题答错：记困惑点，换讲法重讲
    ev = await say(s, "不知道")
    assert of(ev, NoteSaved)[0].note_type == "confusion"
    assert of(ev, TutorMessage)[0].kind == "reteach"
    assert last_state(ev) == "TEACH"

    # 3. 本章讲完进入练习：真题优先，按题号
    ev = await say(s, "继续")
    q = of(ev, QuestionShown)[0]
    assert (q.qid, q.index, q.total, q.qtype) == ("q006", 1, 2, "choice")
    assert last_state(ev) == "PRACTICE"

    # 4. 选择题由规则判分，不调用 Grader
    calls_before = env["brain"].calls.count("grade")
    ev = await say(s, "B")
    g = of(ev, Graded)[0]
    assert (g.qid, g.verdict, g.grader, g.score) == ("q006", "right", "rule", 1.0)
    assert env["brain"].calls.count("grade") == calls_before
    assert of(ev, TutorMessage)[0].kind == "feedback"
    assert of(ev, QuestionShown)[0].qid == "q007"

    # 5. 提示阶梯，然后答错
    ev = await say(s, "给个提示")
    assert of(ev, TutorMessage)[0].kind == "hint"
    ev = await say(s, "A")
    g = of(ev, Graded)[0]
    assert (g.verdict, g.grader) == ("wrong", "rule")
    assert g.reference is not None
    assert g.reference.startswith("B")
    assert g.reference_source == "期末模拟卷.txt p.1"

    # 6. 题做完自动小结：答对过带答案的题 → verified，进入下一章
    assert of(ev, TutorMessage)[-1].kind == "wrapup"
    assert of(ev, NoteSaved)[-1].note_type == "summary"
    assert of(ev, ChapterDone)[0].status == "verified"
    assert s.chapter == 2
    assert last_state(ev) == "TEACH"

    rec = LearnerRecords(env["db"])
    attempts = await rec.attempts(s.enr.id)
    assert [(a.qid, a.verdict, a.hints_used) for a in attempts] == [
        ("q006", "right", 0),
        ("q007", "wrong", 1),
    ]
    assert await rec.open_mistakes(s.enr.id) == {"q007"}
    assert len(await rec.attempts(s.enr.id, kind="check")) == 1


async def test_llm_grading_reveal_and_self_assessment(env: dict[str, Any]) -> None:
    env["brain"].fail_grader_on = {"大概"}
    s = await open_session(env)
    await say(s, "跳到第2章")
    ev = await say(s, "考考我")
    first = of(ev, QuestionShown)[0]
    assert first.qid == "q010"  # 真题优先
    assert first.total == 3

    # 文字填空换了说法：规则判不了，交给 Grader
    ev = await say(s, "LIFO 和 FIFO")
    g = of(ev, Graded)[0]
    assert (g.qid, g.verdict, g.grader) == ("q010", "right", "llm")
    assert of(ev, QuestionShown)[0].qid == "q001"

    # 看答案：记为 skip（进入错题），展示参考答案并讲评
    ev = await say(s, "看答案")
    g = of(ev, Graded)[0]
    assert (g.qid, g.verdict, g.reference) == (
        "q001",
        "skip",
        "C\n解析：1 出栈前 3 已出栈，说明 1、2 仍在栈中且 2 在 1 之上，此时不能先出 1。",
    )
    assert of(ev, TutorMessage)[0].kind == "feedback"

    # Grader 失败：展示参考答案，请学生自评
    ev = await say(s, "大概三个吧")
    assert of(ev, Graded)[0].verdict == "unsure"
    assert last_state(ev) == "SELF"
    ev = await say(s, "嗯？")
    assert of(ev, Notice)[0].text.startswith("请回复")
    ev = await say(s, "对了")
    g = of(ev, Graded)[0]
    assert (g.qid, g.verdict, g.grader) == ("q002", "right", "self")
    assert of(ev, ChapterDone)[0].status == "verified"

    rec = LearnerRecords(env["db"])
    assert await rec.open_mistakes(s.enr.id) == {"q001"}


async def test_ask_and_refusal(env: dict[str, Any]) -> None:
    s = await open_session(env)
    calls = len(env["brain"].calls)
    ev = await say(s, "量子纠缠是什么？")
    assert of(ev, Refused)[0].text == "资料里没有讲到这一点。"
    assert len(env["brain"].calls) == calls  # 资料外的问题不调用模型
    ev = await say(s, "循环队列怎么判满？")
    answer = of(ev, TutorMessage)[0]
    assert answer.kind == "answer"
    assert "第2章_栈和队列.md" in answer.text


async def test_restore_after_restart(env: dict[str, Any]) -> None:
    s = await open_session(env)
    await say(s, "跳到第2章")
    await say(s, "考考我")
    history = list(s.history)
    assert history
    s2 = await open_session(env)
    assert s2.chapter == 2
    assert s2.state == "PRACTICE"
    assert s2.history == history
    start = [ev async for ev in s2.start()]
    assert of(start, QuestionShown)[0].qid == "q010"
    ev = await say(s2, "后进先出；先进先出")
    assert of(ev, Graded)[0].verdict == "right"


async def test_course_done_progress_and_goto(env: dict[str, Any]) -> None:
    s = await open_session(env)
    ev = await say(s, "跳到第9章")
    assert of(ev, Notice)[0].level == "warning"
    await say(s, "跳到第3章")
    ev = await say(s, "这章好了")
    assert of(ev, ChapterDone)[0].status == "covered"
    assert of(ev, CourseDone)
    assert s.state == "DONE"
    ev = await say(s, "继续")
    assert "全部学完" in of(ev, Notice)[0].text
    ev = await say(s, "进度")
    report = of(ev, ProgressReport)[0]
    assert report.current == 3
    assert [c.status for c in report.chapters] == ["todo", "todo", "covered"]
    assert report.chapters[2].questions == 4
    ev = await say(s, "给个提示")
    assert "没有待作答的题" in of(ev, Notice)[0].text
    ev = await say(s, "跳到第3章")  # 复习已学完的章：从头讲
    assert of(ev, TutorMessage)[0].kind == "teach"


async def test_practice_first_for_gap_level(env: dict[str, Any]) -> None:
    s = await open_session(env, level="gap", mode="cram", exam_date="2099-01-01")
    ev = await say(s, "继续")
    assert "先做几道题" in of(ev, Notice)[0].text
    assert of(ev, QuestionShown)[0].qid == "q006"
    await say(s, "A")  # 错
    ev = await say(s, "B")
    # 摸底有错题：回到讲解
    assert any("从头讲" in n.text for n in of(ev, Notice))
    assert of(ev, TutorMessage)[-1].kind == "teach"


async def test_confused_twice_then_move_on(env: dict[str, Any]) -> None:
    soon = (dt.date.today() + dt.timedelta(days=2)).isoformat()
    s = await open_session(env, level="weak", mode="cram", exam_date=soon)  # 补弱 × 1–3 天：不出检查题
    ev = await say(s, "没懂")
    assert "还没开始讲" in of(ev, Notice)[0].text
    ev = await say(s, "继续")
    assert last_state(ev) == "TEACH"
    for _ in range(2):
        ev = await say(s, "没懂")
        assert of(ev, TutorMessage)[0].kind == "reteach"
    ev = await say(s, "还是不懂")
    assert "困惑点" in of(ev, Notice)[0].text
    rec = LearnerRecords(env["db"])
    assert len(await rec.notes(s.enr.id, type="confusion")) == 1


async def test_tutor_failure_is_reported(env: dict[str, Any]) -> None:
    s = await open_session(env)
    brain: Brain = env["brain"]
    original = brain.__call__

    def broken(req: LLMRequest) -> FakeTurn | LLMError:
        if not req.tools:
            return ServerError("tutor down")
        return original(req)

    env["gw"].providers["fake"].responder = broken
    ev = await say(s, "继续")
    assert of(ev, Notice)[0].level == "error"
