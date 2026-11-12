"""教学规则：分段、档位、意图识别、策略矩阵、选题。"""

from __future__ import annotations

import datetime as dt

import pytest

from examprep.knowledge.evidence import EvidenceChunk
from examprep.pedagogy.intent import Awaiting, classify
from examprep.pedagogy.parts import part_chars, pick_profile, split_parts
from examprep.pedagogy.picker import Candidate, pick_questions
from examprep.pedagogy.strategy import bucket_of, days_left, strategy_for

# ---------------------------------------------------------------- 分段


def chunk(i: int, n: int) -> EvidenceChunk:
    return EvidenceChunk(f"E{i}", "a.md", 1, 1, i, "字" * n, 0, n, n)


def test_split_parts_by_target() -> None:
    chunks = [chunk(i, 600) for i in range(7)]
    parts = split_parts(chunks, 1800)
    assert [len(p) for p in parts] == [3, 3, 1]
    assert [c.id for p in parts for c in p] == [c.id for c in chunks]  # 顺序不变、不丢块
    assert split_parts([chunk(0, 5000)], 1800) == [[chunk(0, 5000)]]  # 超长块自成一段
    assert split_parts([], 1800) == []


@pytest.mark.parametrize(
    ("window", "want"), [(8192, "small"), (32_000, "small"), (128_000, "standard"), (1_048_576, "large")]
)
def test_pick_profile(window: int, want: str) -> None:
    assert pick_profile(window) == want


def test_part_chars() -> None:
    assert part_chars("small", "zh") == 1800
    assert part_chars("large", "en") == 12000
    assert part_chars("unknown", "zh") == 3000


# ---------------------------------------------------------------- 意图


@pytest.mark.parametrize(
    ("text", "awaiting", "kind", "chapter"),
    [
        ("继续", None, "continue", None),
        ("好的。", None, "continue", None),
        ("", None, "continue", None),
        ("ok", "check", "continue", None),
        ("跳到第3章", None, "goto", 3),
        ("第十二章", None, "goto", 12),
        ("go to chapter 4", None, "goto", 4),
        ("进度", None, "progress", None),
        ("学到哪了？", None, "progress", None),
        ("给个提示", "answer", "hint", None),
        ("hint", None, "hint", None),
        ("看答案", "answer", "reveal", None),
        ("我不会", "answer", "reveal", None),
        ("跳过", "answer", "skip", None),
        ("没懂", None, "confused", None),
        ("换个讲法", "check", "confused", None),
        ("这章好了", None, "done", None),
        ("考考我", None, "quiz", None),
        ("压缩上下文", "answer", "compact", None),  # 明确说法任何时候都识别
        ("/compact", "check", "compact", None),
        ("压缩", None, "compact", None),
        ("压缩", "answer", "answer", None),  # 等作答时单说“压缩”是答案
        ("压缩", "check", "check_answer", None),
        ("quiz me", None, "quiz", None),
        ("B", "answer", "answer", None),
        ("答案是 a b c * +", "answer", "answer", None),
        ("后进先出", "check", "check_answer", None),
        ("为什么顺序表插入要移动元素", None, "ask", None),
        ("循环队列怎么判满？", None, "ask", None),
        ("What is a heap?", None, "ask", None),
        ("栈", None, "continue", None),
        ("请把二叉树的三种遍历都写一下", None, "ask", None),
    ],
)
def test_classify(text: str, awaiting: Awaiting, kind: str, chapter: int | None) -> None:
    intent = classify(text, awaiting=awaiting)
    assert intent.kind == kind
    assert intent.chapter == chapter


# ---------------------------------------------------------------- 策略


def test_days_left_and_bucket() -> None:
    today = dt.date(2026, 11, 11)
    assert days_left("2026-11-14", today) == 3
    assert days_left("2026-11-01", today) == 0
    assert days_left(None, today) is None
    assert days_left("not a date", today) is None
    assert bucket_of("cram", 0) == "1d"
    assert bucket_of("cram", 3) == "3d"
    assert bucket_of("cram", 6) == "7d"
    assert bucket_of("cram", 30) == "long"
    assert bucket_of("longterm", 1) == "long"
    assert bucket_of("cram", None) == "long"


def test_strategy_matrix() -> None:
    today = dt.date(2026, 11, 11)
    s = strategy_for("zero", "cram", "2026-11-12", today)
    assert (s.check_every, s.questions_per_chapter, s.hint_ladder, s.bucket) == (0, 1, False, "1d")
    s = strategy_for("zero", "longterm", None, today)
    assert (s.check_every, s.hint_ladder, s.practice_first) == (1, True, False)
    s = strategy_for("gap", "cram", "2026-11-15", today)
    assert s.practice_first
    assert s.check_every == 0
    assert strategy_for("bogus", "cram", None, today) == strategy_for("zero", "cram", None, today)


# ---------------------------------------------------------------- 选题


POOL = [
    Candidate("q001", 1, False),
    Candidate("q002", 1, True),
    Candidate("q003", 1, False),
    Candidate("q004", 2, True),
]


def test_pick_order() -> None:
    assert pick_questions(POOL, chapter=1, last_verdicts={}, open_mistakes=set(), count=3) == [
        "q002",
        "q001",
        "q003",
    ]
    picked = pick_questions(
        POOL, chapter=1, last_verdicts={"q001": "wrong", "q002": "right"}, open_mistakes={"q001"}, count=3
    )
    assert picked == ["q001", "q003", "q002"]
    assert pick_questions(POOL, chapter=None, last_verdicts={}, open_mistakes=set(), count=2) == [
        "q002",
        "q004",
    ]
    assert pick_questions(POOL, chapter=1, last_verdicts={}, open_mistakes=set(), exclude={"q002"}) == [
        "q001"
    ]
    assert pick_questions(POOL, chapter=9, last_verdicts={}, open_mistakes=set()) == []
