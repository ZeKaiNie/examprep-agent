"""移植自 Skill tests/test_questions.py、test_questions_labels.py、test_grade_profiles.py（判分部分）与 index 测试。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from examprep.ingest.chapters import Chapter
from examprep.ingest.extract import Page, Source
from examprep.ingest.grading import answer_key, grade_answer
from examprep.ingest.questions import extract_questions, heads, pair_key
from examprep.ingest.text import tokenize
from examprep.knowledge.bm25 import BM25


def src(rel: str, kind: str, pages: list[str], scan: Sequence[int] = ()) -> Source:
    s = Source("/m/" + rel, rel, kind)
    s.pages = [Page(i, t, scan=(i in scan)) for i, t in enumerate(pages, 1)]
    return s


def chapter(n: int, title: str, text: str, file: str | None = None) -> Chapter:
    return Chapter(n, title, blocks=[(file or f"lec{n}.md", 1, text)])


CHAPTERS = [
    chapter(
        1,
        "Stacks",
        "A stack is last in first out (LIFO). push pop top overflow underflow. 栈 后进先出 入栈 出栈 栈顶",
    ),
    chapter(
        2,
        "Queues",
        "A queue is first in first out (FIFO). enqueue dequeue front rear circular. 队列 先进先出 循环队列 front rear",
    ),
]

# ---------------------------------------------------------------- 抽题


def test_inline_answers_sections_and_types() -> None:
    text = (
        "一、选择题\n\n1. 栈的特点是\nA. 先进先出\nB. 后进先出\n答案：B\n解析：LIFO\n\n"
        "2. 判断题：队列先进先出\n答案：正确\n\n"
        "二、填空题\n\n1. 循环队列判空条件是____。\n答案：front == rear\n\n"
        "2. 简述 dequeue 的过程。\n"
    )
    items = extract_questions([src("作业1.txt", "homework", [text])], CHAPTERS)
    assert [q.id for q in items] == ["q001", "q002", "q003", "q004"]
    assert items[0].type == "choice"
    assert items[0].options == ["A. 先进先出", "B. 后进先出"]
    assert items[0].answer == "B\n解析：LIFO"
    assert items[1].type == "true_false"
    assert items[2].type == "fill_blank"
    assert items[2].answer == "front == rear"
    assert "二、填空题" not in (items[1].answer or "")
    assert items[3].answer is None
    assert items[3].type == "subjective"
    assert items[0].chapter == 1
    assert items[2].chapter == 2
    assert items[2].chapter_guessed
    assert items[0].to_dict()["source"]["kind"] == "homework"


def test_paired_solution_file() -> None:
    q = src(
        "hw1.txt",
        "homework",
        ["Problem 1. [5 points] Is a stack LIFO?\n(a) explain\n\nProblem 2. Define a queue."],
    )
    s = src(
        "hw1_solutions.txt",
        "solution",
        [
            "Problem 1. [5 points] Is a stack LIFO?\n(a) explain\nSolution: Yes, last in first out.\n\n"
            "Problem 2. Define a queue.\nSolution: FIFO structure."
        ],
    )
    items = extract_questions([q, s], CHAPTERS)
    assert len(items) == 2
    assert items[0].answer == "Solution: Yes, last in first out."
    assert items[0].answer_source is not None
    assert items[0].answer_source.file == "hw1_solutions.txt"
    assert items[0].answer_source.page == 1
    assert items[0].points == 5
    assert items[1].answer == "Solution: FIFO structure."


def test_pair_key() -> None:
    assert pair_key("MIT6_006S20_q1_sol.pdf") == pair_key("MIT6_006S20_q1.pdf")
    assert pair_key("作业2答案.txt") == pair_key("作业2.txt")
    assert pair_key("hw1.pdf") != pair_key("hw2.pdf")
    assert pair_key("hw2 (4)(1).pdf") == "hw2"
    assert pair_key("homework2solutions.pdf") == "hw2"
    assert pair_key("hw5-solutions(1).pdf") == "hw5"
    assert pair_key("hw1solution.pdf") == "hw1"
    assert pair_key("Assignment 3 answers.pdf") == "hw3"


def test_sequence_rule_ignores_numbered_lists_inside_answers() -> None:
    text = "Problem 1. Sort the array.\nSolution: steps\n1. split\n2. merge\n\nProblem 2. Hash the keys."
    items = extract_questions([src("exam.txt", "exam", [text])], CHAPTERS)
    assert [q.number for q in items] == [1, 2]


def test_explicit_chapter_hint_wins() -> None:
    items = extract_questions(
        [src("hw.txt", "homework", ["1. (Chapter 2) What is a stack?\n答案：LIFO"])], CHAPTERS
    )
    assert items[0].chapter == 2
    assert not items[0].chapter_guessed


def test_zh_ti_style_and_paren_style() -> None:
    items = extract_questions(
        [src("练习.txt", "homework", ["第一题 栈和队列有什么区别？\n第二题 什么是循环队列？"])], CHAPTERS
    )
    assert [q.number for q in items] == [1, 2]
    items = extract_questions(
        [src("x.txt", "homework", ["(1) 什么是栈的上溢现象\n(2) 什么是队列的假溢出"])], CHAPTERS
    )
    assert [q.label for q in items] == ["1", "2"]


def test_no_chapters_leaves_chapter_none() -> None:
    items = extract_questions([src("hw.txt", "homework", ["1. What is a stack exactly?"])], [])
    assert items[0].chapter is None


LABEL_CHAPTERS = [
    chapter(1, "Sets", "sets union intersection venn", "ch01.pdf"),
    chapter(2, "Sequential", "tree diagram sequential", "ch02.pdf"),
]


def test_textbook_labels_pair_and_set_chapter() -> None:
    hw = src(
        "hw1 (4)(1).pdf",
        "homework",
        [
            "EEC161 Homework 1\n1. Problem 1.1.2 (see the book)\n2. Problem 1.2.1\n3. Problem 2.1.3",
            "a No N and M are not mutually exclusive because",  # 学生的扫描答题页（OCR）
            "more handwriting",
        ],
        scan=(1, 2, 3),
    )
    sol = src(
        "hw1solution.pdf",
        "solution",
        [
            "Homework 1 Solutions\nYates and Goodman 3e Solution Set: 1.1.2, 1.2.1, and\n2.1.3\n"
            "Problem 1.1.2 Solution\nBased on the Venn diagram, N and M are not mutually exclusive.\n"
            "Problem 1.2.1 Solution\nThe sample space has four outcomes.\n"
            "Problem 2.1.3 Solution\nUse a tree diagram."
        ],
    )
    items = extract_questions([hw, sol], LABEL_CHAPTERS)
    assert [q.label for q in items] == ["1.1.2", "1.2.1", "2.1.3"]
    assert items[0].answer == "Based on the Venn diagram, N and M are not mutually exclusive."
    assert items[2].answer == "Use a tree diagram."
    assert [q.chapter for q in items] == [1, 1, 2]
    assert not any(q.chapter_guessed for q in items)
    # 手写 OCR 文本不会混进题目
    assert "handwriting" not in items[2].question
    assert "mutually exclusive because" not in items[2].question
    assert heads(hw)[0] == (1, "1. Problem 1.1.2 (see the book)", "1.1.2")


def test_dotted_label_is_not_a_list_item() -> None:
    sol = src(
        "hw9solutions.pdf",
        "solution",
        ["7.3.6, 7.4.2, and 9.1.2\nProblem 7.3.6 Solution\nRandom variables X and Y have joint PDF."],
    )
    items = extract_questions([sol], LABEL_CHAPTERS)
    assert len(items) == 1
    assert items[0].label == "7.3.6"
    assert items[0].question == "Random variables X and Y have joint PDF."


# ---------------------------------------------------------------- BM25


def test_bm25_hit_miss_and_cjk() -> None:
    docs = [
        "Counting sort runs in linear time when keys are small integers.",
        "循环队列用数组实现，判满条件是 (rear + 1) % MaxSize == front。",
    ]
    bm = BM25([tokenize(d) for d in docs])
    assert bm.best(tokenize("counting sort linear time"))[0][0] == 0
    assert bm.best(tokenize("quantum entanglement")) == []
    assert bm.best(tokenize("循环队列 判满"))[0][0] == 1
    assert BM25([]).best(["x"]) == []


# ---------------------------------------------------------------- 判分

CHOICE_ANSWER = "B\n解析：插入位置之后的 n - i + 1 个元素都要后移。"
CHOICE_OPTIONS = ["A. n - i", "B. n - i + 1", "C. i", "D. n"]


@pytest.mark.parametrize(
    ("answer", "qtype", "options", "want"),
    [
        (CHOICE_ANSWER, "choice", CHOICE_OPTIONS, ("choice", frozenset("B"))),
        ("答案：BD", "choice", ["A. x", "B. y", "C. z", "D. w"], ("choice", frozenset("BD"))),
        (
            "The correct answer is (C) because ...",
            "choice",
            ["A. x", "B. y", "C. z"],
            ("choice", frozenset("C")),
        ),
        ("正确", "true_false", [], ("truth", True)),
        ("False. The heap property ...", "subjective", [], ("truth", False)),
        ("3\n解析：(1 - 4 + 6) % 6 = 3", "fill_blank", [], ("number", (3.0, 0))),
        ("Answer: 0.25", "subjective", [], ("number", (0.25, 2))),
        ("1/4", "fill_blank", [], ("number", (0.25, 0))),
        ("后进先出；先进先出", "fill_blank", [], ("text", "后进先出先进先出")),
        ("n", "fill_blank", [], ("text", "n")),
    ],
)
def test_keys_that_can_be_compared(answer: str, qtype: str, options: list[str], want: object) -> None:
    assert answer_key(answer, qtype, options) == want


@pytest.mark.parametrize(
    ("answer", "qtype", "options"),
    [
        ("Solution: O(n2)", "subjective", []),
        ("Solution: We present both expected and worst-case solutions", "subjective", []),
        ("Solution: A", "subjective", []),
        ("x = 3 and y = 4", "subjective", []),
        ("前序遍历为 ABDECF，中序遍历为 DBEAFC", "subjective", []),
        (None, "choice", ["A. x", "B. y"]),
        ("见课本第 3 章", "choice", ["A. x", "B. y"]),
        ("1/0", "subjective", []),  # 除零：不是数
    ],
)
def test_no_key_when_comparison_would_be_wrong(answer: str | None, qtype: str, options: list[str]) -> None:
    assert answer_key(answer, qtype, options) is None


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("B", "right"),
        ("b", "right"),
        ("我选 B", "right"),
        ("I'd pick B.", "right"),
        ("n - i + 1", "right"),
        ("A", "wrong"),
        ("it's a tricky one, A", "wrong"),
        ("I am not sure", "unsure"),
        ("", "unsure"),
    ],
)
def test_grade_choice(reply: str, verdict: str) -> None:
    assert grade_answer(CHOICE_ANSWER, "choice", CHOICE_OPTIONS, reply)[0] == verdict


def test_grade_choice_shows_key() -> None:
    assert grade_answer(CHOICE_ANSWER, "choice", CHOICE_OPTIONS, "B")[1] == "B"


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("错", "right"),
        ("不对", "right"),
        ("×", "right"),
        ("false", "right"),
        ("对的", "wrong"),
        ("正确", "wrong"),
        ("不知道呢，可能吧大概是这样子", "unsure"),
    ],
)
def test_grade_true_false(reply: str, verdict: str) -> None:
    assert grade_answer("错误", "true_false", [], reply)[0] == verdict


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("1/3", "right"),
        ("0.33", "right"),
        ("33.3%", "right"),
        ("0.4", "wrong"),
        ("about 0.333 seconds", "right"),
        ("0.3 or 0.4", "unsure"),
        ("one third", "unsure"),
    ],
)
def test_grade_numbers(reply: str, verdict: str) -> None:
    assert grade_answer("0.333\nExplanation ...", "fill_blank", [], reply)[0] == verdict


def test_grade_integers_with_units() -> None:
    assert grade_answer("11", "fill_blank", [], "11 个")[0] == "right"
    assert grade_answer("11", "fill_blank", [], "12")[0] == "wrong"


def test_text_fill_in_is_never_marked_wrong_automatically() -> None:
    assert grade_answer("后进先出；先进先出", "fill_blank", [], "后进先出、先进先出")[0] == "right"
    assert grade_answer("后进先出；先进先出", "fill_blank", [], "LIFO, FIFO")[0] == "unsure"


def test_open_questions_are_left_to_the_tutor() -> None:
    assert grade_answer("Solution: We use a heap ...", "subjective", [], "a heap") == ("unsure", None)
