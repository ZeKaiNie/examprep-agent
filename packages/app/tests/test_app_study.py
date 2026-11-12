"""学习会话端到端：假模型按请求内容回复（Tutor / Grader / Verifier），在中文样例上走完整流程。"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from conftest import SAMPLES
from fakes import Brain

from examprep.ingest.pipeline import ingest
from examprep.learner.records import LearnerRecords
from examprep.settings import CompactionPolicy
from examprep.store import Database
from examprep.study.events import (
    ChapterDone,
    Compacted,
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
from examprep.study.session import StudyOptions, StudySession, restore_segment
from examprep_llm import (
    AssistantMessage,
    Gateway,
    LLMError,
    LLMRequest,
    ServerError,
    SystemMessage,
    UserMessage,
)
from examprep_llm.testing import FakeClock, FakeProvider, FakeTurn
from examprep_runtime.session import SQLiteEventStore


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


async def open_session(
    env: dict[str, Any],
    *,
    compaction: CompactionPolicy | None = None,
    context_window: int = 128_000,
    **opts: Any,
) -> StudySession:
    return await StudySession.open(
        env["db"],
        env["gw"],
        env["course"],
        events=env["store"],
        options=StudyOptions(**opts),
        compaction=compaction,
        context_window=context_window,
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
    # “看答案”是指令不是答案：讲评按未作答处理，提示词里不出现“判定是：skip”
    kind, prompt = env["brain"].prompts[-1]
    assert kind == "feedback"
    assert "学生没有作答" in prompt
    assert "（未作答）" in prompt
    assert "skip" not in prompt

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


def _tutor_requests(env: dict[str, Any]) -> list[LLMRequest]:
    fake: FakeProvider = env["gw"].providers["fake"]
    return [r for r in fake.requests if not r.tools]


def _last_user(request: LLMRequest) -> str:
    last = request.messages[-1]
    assert isinstance(last, UserMessage)
    return last.text


async def test_tutor_requests_are_append_only_within_a_chapter(env: dict[str, Any]) -> None:
    """缓存前缀（R7 真实验收发现的问题）：同一章内每次 Tutor 请求都是上一次请求的前缀扩展。

    上一次请求（含资料的完整用户消息）+ 模型原样输出 = 下一次请求去掉最后一条用户消息。
    """
    s = await open_session(env)
    for text in [
        "继续",
        "顺序表要移动元素",
        "顺序表插入为什么慢？",
        "量子纠缠是什么？",
        "这里没懂",
        "继续",
        "B",
    ]:
        await say(s, text)
    chapter1 = [r for r in _tutor_requests(env) if "第 1 章" in _last_user(r)]
    assert len(chapter1) >= 5
    for prev, nxt in itertools.pairwise(chapter1):
        assert nxt.messages[: len(prev.messages)] == prev.messages
    systems = [m for m in chapter1[-1].messages if isinstance(m, SystemMessage)]
    assert "未解决的错题" not in "".join(m.content for m in systems)  # 会变的状态不进系统消息
    assert all("## 学习者状态" in _last_user(r) for r in chapter1)
    # 讲过的资料不再重发：之后的轮次只列编号
    assert "<<<MATERIAL" in _last_user(chapter1[0])
    assert any("在前面已经给出" in _last_user(r) for r in chapter1[1:])
    # 历史里是模型的原样输出（带 [[E1]] 标记），不是校验后的文本
    assert any("[[E1]]" in m.text for m in s.history if isinstance(m, AssistantMessage))


async def test_new_chapter_starts_a_new_segment_and_resume_restores_labels(env: dict[str, Any]) -> None:
    s = await open_session(env)
    await say(s, "继续")
    await say(s, "跳到第2章")
    labels = s.cmap.labels()
    assert s.segment_chapter == 2
    assert labels  # 第 2 章的资料重新从 E1 编号
    assert labels[0] == "E1"
    first = next(r for r in _tutor_requests(env) if "第 2 章" in _last_user(r))
    assert not any(isinstance(m, AssistantMessage) for m in first.messages)  # 新段不带上一章的对话

    s2 = await open_session(env)
    assert s2.segment_chapter == 2
    assert s2.history == s.history
    assert [s2.cmap.resolve(lb) for lb in labels] == [s.cmap.resolve(lb) for lb in labels]


def _system_text(request: LLMRequest) -> str:
    return "\n".join(m.content for m in request.messages if isinstance(m, SystemMessage))


async def test_auto_compaction_reuses_prefix_and_carries_summary(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await open_session(env)
    await say(s, "继续")  # 讲解 + 检查题，本段有 4 条消息
    assert len(s.history) == 4
    await say(s, "顺序表要移动元素")  # 回答检查题（Grader，不进 Tutor 对话）
    monkeypatch.setattr(s, "compact_limit", lambda: 1)  # 下一次调用 Tutor 前就超过阈值
    ev = await say(s, "顺序表插入为什么慢？")
    done = of(ev, Compacted)[0]
    assert (done.manual, done.ok) == (False, True)
    assert done.summary.startswith("摘要：讲过线性表的定义")
    assert "[[" not in done.summary  # 摘要里的引用标记去掉
    assert done.tokens_after < done.tokens_before

    # 压缩请求 = 上一次 Tutor 请求 + 模型输出 + 末尾一条压缩指令：前缀完全相同，能命中缓存
    reqs = _tutor_requests(env)
    k = next(i for i, r in enumerate(reqs) if _last_user(r).startswith("暂停讲课"))
    prev, comp = reqs[k - 1], reqs[k]
    assert comp.messages[: len(prev.messages)] == prev.messages
    assert len(comp.messages) == len(prev.messages) + 2
    # 压缩后的新段：摘要进系统消息，历史从空开始，本轮照常回答
    nxt = reqs[k + 1]
    assert "这一章前面对话的摘要" in _system_text(nxt)
    assert "讲过线性表的定义" in _system_text(nxt)
    assert not any(isinstance(m, AssistantMessage) for m in nxt.messages)
    assert of(ev, TutorMessage)[0].kind == "answer"
    assert len(s.history) == 2

    # 断点恢复：摘要随段一起恢复，系统消息与压缩后完全相同
    s2 = await open_session(env)
    assert s2.segment_summary == s.segment_summary
    assert s2._system() == s._system()  # pyright: ignore[reportPrivateUsage]
    events = await env["store"].events(s.enr.session_id)
    assert [e.payload["reason"] for e in events if e.type == "segment"] == ["chapter", "compact"]


async def test_manual_compaction(env: dict[str, Any]) -> None:
    s = await open_session(env)
    ev = await say(s, "压缩上下文")
    assert "还很短" in of(ev, Notice)[0].text
    await say(s, "继续")
    assert s.state == "CHECK"
    ev = await say(s, "压缩")  # 等回答检查题时，单说“压缩”算作回答
    assert of(ev, Compacted) == []
    await say(s, "顺序表插入为什么慢？")
    ev = await say(s, "压缩上下文")
    done = of(ev, Compacted)[0]
    assert (done.manual, done.ok) == (True, True)
    assert s.history == []
    assert s.segment_summary == done.summary

    # 手动压缩失败：对话保持原样
    await say(s, "链表呢？")
    history = list(s.history)
    env["brain"].fail_compact = True
    ev = await say(s, "/compact")
    assert of(ev, Compacted)[0].ok is False
    assert s.history == history


async def test_compaction_failure_or_disabled_starts_a_new_segment(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await open_session(env)
    await say(s, "继续")
    await say(s, "顺序表要移动元素")
    monkeypatch.setattr(s, "compact_limit", lambda: 1)
    env["brain"].fail_compact = True
    ev = await say(s, "顺序表插入为什么慢？")
    assert of(ev, Compacted)[0].ok is False
    assert "已开新对话" in of(ev, Notice)[-1].text
    assert len(s.history) == 2  # 只剩本轮

    # 关闭自动压缩：不发压缩请求，直接开新段
    s2 = await open_session(env, compaction=CompactionPolicy(auto=False))
    monkeypatch.setattr(s2, "compact_limit", lambda: 1)
    calls = env["brain"].calls.count("compact")
    ev = await say(s2, "链表插入为什么快？")
    assert env["brain"].calls.count("compact") == calls
    assert "自动压缩已关闭" in of(ev, Notice)[0].text
    assert len(s2.history) == 2


def test_compact_limit_respects_window_and_policy() -> None:
    p = CompactionPolicy()
    assert p.threshold(1_048_576) == 60_000  # 大窗口：绝对上限先到
    assert p.threshold(32_000) == 22_400  # 小窗口：70%
    with pytest.raises(ValueError, match="ratio"):
        CompactionPolicy(ratio=1.0)
    with pytest.raises(ValueError, match="max_tokens"):
        CompactionPolicy(max_tokens=10)


async def test_compact_limit_leaves_room_for_materials(env: dict[str, Any]) -> None:
    s = await open_session(env, context_window=32_000)
    assert s.compact_limit() <= 32_000 - s.evidence_budget - s.system_budget
    big = await open_session(env, context_window=1_048_576)
    assert big.compact_limit() == 60_000
    off = await open_session(env, context_window=1_048_576, compaction=CompactionPolicy(auto=False))
    assert off.compact_limit() > 60_000


async def test_new_chapter_gets_earlier_chapter_summaries(env: dict[str, Any]) -> None:
    s = await open_session(env)
    await say(s, "继续")
    await say(s, "这章好了")  # 第 1 章小结，进入第 2 章
    assert s.chapter == 2
    await say(s, "继续")
    first = next(r for r in _tutor_requests(env) if "第 2 章" in _last_user(r))
    system = _system_text(first)
    assert "前面几章的小结" in system
    assert "第 1 章《线性表》" in system
    assert "要点一" in system
    assert "p.1" not in system  # 渲染好的出处去掉，模型不会照抄页码


async def test_verifier_failure_is_retried_then_reported(env: dict[str, Any]) -> None:
    brain = env["brain"]
    s = await open_session(env)
    brain.fail_verify = 1  # 第一次失败，重试成功
    ev = await say(s, "循环队列怎么判满？")
    assert of(ev, Verified)
    assert not [n for n in of(ev, Notice) if n.level == "warning"]
    brain.fail_verify = 2  # 两次都失败：提示学生这条没核对过
    ev = await say(s, "循环队列怎么判空？")
    assert of(ev, Verified) == []
    assert "没能完成逐句核对" in of(ev, Notice)[0].text


async def test_transcript_only_has_what_the_student_said(env: dict[str, Any]) -> None:
    """真实验收发现：系统替学生补“这里没懂”“这章好了”，压缩摘要就把学生没说过的话当成了事实。"""
    s = await open_session(env)
    await say(s, "继续")
    await say(s, "不知道")  # 检查题答错 → 同一轮里重讲
    reteach = next(r for r in _tutor_requests(env) if "换一种讲法" in _last_user(r))
    assert "学生说：不知道" in _last_user(reteach)
    assert "这里没懂" not in _last_user(reteach)
    first = _tutor_requests(env)[0]
    assert "学生说：继续" in _last_user(first)
    check = next(r for r in _tutor_requests(env) if "检查理解" in _last_user(r))
    assert "学生说" not in _last_user(check)  # 同一轮的第二次调用：学生没有再说话


async def test_segment_is_dropped_when_evidence_changed(env: dict[str, Any]) -> None:
    s = await open_session(env)
    await say(s, "继续")
    assert s.history
    events = await env["store"].events(s.enr.session_id)
    s.course.index.chunks.pop(0)  # 模拟重新导入后证据 id 变了
    chapter, history, cmap, summary = restore_segment(events, s.course)
    assert (chapter, history, len(cmap), summary) == (None, [], 0, "")


async def test_empty_tutor_reply_is_retried_once(env: dict[str, Any]) -> None:
    s = await open_session(env)
    brain = env["brain"]
    brain.empty_next = 1
    ev = await say(s, "循环队列怎么判满？")
    assert brain.calls.count("answer") == 2
    assert of(ev, TutorMessage)[0].text.startswith("循环队列判满")

    # 连续两次空回复：提示学生重说，不输出空消息、不写入对话历史
    brain.empty_next = 2
    history = len(s.history)
    ev = await say(s, "循环队列怎么判空？")
    assert of(ev, TutorMessage) == []
    assert of(ev, Notice)[0].level == "error"
    assert len(s.history) == history


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
