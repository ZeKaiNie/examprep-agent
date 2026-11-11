"""从作业、测验、试卷中抽题目（有答案时一并抽出）。移植自 Skill v5.3 questions.py。

题库只包含资料里有的题。找不到答案时 ``answer`` 为 None，讲解时必须标明答案是 AI 生成的。

题号风格：``Problem 3`` / ``Q3`` / ``3.`` / ``第3题`` / ``(3)`` 顺序编号，以及教材题号 ``Problem 1.3.10``
（与答案文件里的 ``Problem 1.3.10 Solution`` 配对）。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..knowledge.bm25 import BM25
from .chapters import Chapter
from .extract import Source
from .text import chinese_numeral, tokenize

QUESTION_KINDS = ("homework", "exam", "solution")

_LABEL = r"(\d{1,3}(?:\.\d{1,3}){0,3})"
_ZH_NUM = r"([0-9一二三四五六七八九十]+)"
# (风格, 正则)；只认行首匹配
_STARTS: list[tuple[str, re.Pattern[str]]] = [
    (
        "problem",
        re.compile(rf"^\s*(?:problem|question|exercise|q)\s*[.#]?\s*{_LABEL}\s*[.):：、\-]*\s*", re.I),
    ),
    ("zh_ti", re.compile(rf"^\s*第\s*{_ZH_NUM}\s*[题问]\s*[.:：、]?\s*")),
    ("dot", re.compile(r"^\s*(\d{1,3})\s*[.、．)](?![.\d])\s*(?=\S)")),
    ("paren", re.compile(r"^\s*[（(](\d{1,3})[)）]\s*(?=\S)")),
]
_EXPLICIT = ("problem", "zh_ti")
_INNER_LABEL = re.compile(rf"(?:problem|exercise|question|题)\s*{_LABEL}(?![.\d])", re.I)
_SOLUTION_TAIL = re.compile(r"\s*(?:solution|answer|解答|答案)s?\s*[:：]?\s*$", re.I)
_ANSWER_MARK = re.compile(
    r"^\s*(?:solution|answer|ans|解答|答案|参考答案|解析|解|答)\s*[:：.．]\s*", re.I | re.M
)
_ANSWER_INLINE = re.compile(
    r"(?:^|\n)\s*(?:solution|answer|ans|解答|答案|参考答案|解析|解|答)\s*[:：.．]", re.I
)
_OPTION = re.compile(r"^\s*[（(]?([A-H])[)）.．、]\s*(.+)$")
_POINTS = re.compile(r"\[\s*(\d+)\s*(?:points?|pts?|marks?)\s*\]|[（(]\s*(\d+)\s*分\s*[)）]", re.I)
_CHAPTER_HINT = re.compile(
    r"(?:chapter|lecture|ch\.?)\s*(\d{1,3})|第\s*([0-9一二三四五六七八九十]+)\s*[章讲]", re.I
)
_SECTION_HEAD = re.compile(
    r"^\s*[一二三四五六七八九十]+\s*[、.．]\s*\S+题|^\s*(?:part|section)\s+[a-z0-9]+\b", re.I
)
_SOL_TOKENS = re.compile(r"sol(?:ution)?s?|answers?|key|答案|解答|参考答案|题解", re.I)


@dataclass(slots=True)
class QuestionRef:
    file: str
    page: int
    head: str
    kind: str | None = None  # 只有题面引用带文件类型


@dataclass(slots=True)
class QuestionItem:
    number: int | None
    label: str
    question: str
    options: list[str]
    answer: str | None
    type: str  # choice / true_false / fill_blank / subjective
    points: int | None
    source: QuestionRef
    answer_source: QuestionRef | None
    chapter: int | None
    chapter_guessed: bool = False
    id: str = ""
    figures: list[str] = field(default_factory=list[str])  # 题面裁图（工作区相对路径）
    answer_figures: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class QBlock:
    number: int | None  # 顺序规则用的序号
    label: str  # 教材题号（1.3.10）或序号字符串
    page: int
    head: str
    lines: list[str]


def pair_key(rel: str) -> str:
    """``hw2 (4)(1).pdf``、``homework2solutions.pdf``、``hw2-solutions(1).pdf`` 都归一为 ``hw2``。"""
    name = os.path.splitext(os.path.basename(rel))[0].lower()
    name = re.sub(r"\(\d+\)", "", name)
    name = re.sub(r"homework|assignment|作业|problem\s*set|pset", "hw", name)
    name = _SOL_TOKENS.sub("", name)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", name)


def blocks(src: Source) -> list[QBlock]:
    """把文件切成带编号的题块。同一文件既有显式风格（Problem N、第N题）又有列表编号时，显式风格优先。"""
    explicit = _blocks_with(src, [s for s in _STARTS if s[0] in _EXPLICIT])
    if explicit:
        return explicit
    return _blocks_with(src, [s for s in _STARTS if s[0] not in _EXPLICIT])


def _blocks_with(src: Source, starts: list[tuple[str, re.Pattern[str]]]) -> list[QBlock]:
    out: list[QBlock] = []
    style: str | None = None
    expect = 1
    cur: QBlock | None = None
    for page in src.pages:
        if page.scan:
            # 扫描页的 OCR 文本：只保留本页题号之后的行，手写答题页永远不会续接上一道印刷题
            cur = None
        for line in page.text.split("\n"):
            hit: tuple[str, int | None, str | None, str] | None = None
            for st, rx in starts:
                if style is not None and st != style:
                    continue
                m = rx.match(line)
                if not m:
                    continue
                raw = m.group(1)
                if st == "problem" and "." in raw:
                    hit = (st, None, raw, line[m.end() :])  # 教材题号：不走顺序规则
                    break
                n = chinese_numeral(raw)
                if n is None:
                    continue
                if n in (expect, 1) or (cur is not None and cur.number is not None and n == cur.number + 1):
                    hit = (st, n, None, line[m.end() :])
                break
            if _SECTION_HEAD.match(line):
                cur, expect = None, 1  # “二、填空题” / “Part B”：重新编号，标题本身不是内容
            elif hit:
                st, n, label, rest = hit
                style = style or st
                if label is None:
                    inner = _INNER_LABEL.search(rest)
                    label = inner.group(1) if inner and "." in inner.group(1) else str(n)
                rest = _SOLUTION_TAIL.sub("", rest) if _SOLUTION_TAIL.search(rest) else rest
                cur = QBlock(n, label, page.number, line.strip(), [rest])
                out.append(cur)
                expect = (n + 1) if n is not None else expect
            elif cur is not None:
                cur.lines.append(line)
    return out


def heads(src: Source) -> list[tuple[int, str, str]]:
    """文件中每个题块的 (页, 题头行, 题号)，按文档顺序。"""
    return [(b.page, b.head, b.label) for b in blocks(src)]


def _split_answer(text: str) -> tuple[str, str | None]:
    m = _ANSWER_INLINE.search(text)
    if not m:
        return text.strip(), None
    q = text[: m.start()].strip()
    a = _ANSWER_MARK.sub("", text[m.start() :], count=1).strip()
    return q, (a or None)


def _strip_shared_lines(sol_text: str, q_text: str) -> str | None:
    qset = {" ".join(ln.split()) for ln in q_text.split("\n") if ln.strip()}
    keep = [ln for ln in sol_text.split("\n") if ln.strip() and " ".join(ln.split()) not in qset]
    return "\n".join(keep).strip() or None


def _options(text: str) -> tuple[list[str], str]:
    opts: list[str] = []
    rest: list[str] = []
    for line in text.split("\n"):
        m = _OPTION.match(line)
        if m and (not opts or ord(m.group(1)) == ord(opts[-1][0]) + 1):
            opts.append(f"{m.group(1)}. {m.group(2).strip()}")
        else:
            rest.append(line)
    if len(opts) >= 2:
        return opts, "\n".join(rest).strip()
    return [], text


def question_type(text: str, options: list[str]) -> str:
    if options:
        return "choice"
    low = text.lower()
    if re.search(r"true\s+or\s+false|判断(?:题|正误|对错)|正确还是错误|对还是错", low):
        return "true_false"
    if "____" in text or "___" in text or re.search(r"[（(]\s{2,}[)）]", text):
        return "fill_blank"
    return "subjective"


def _chapter_hint(text: str) -> int | None:
    m = _CHAPTER_HINT.search(text[:300])
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else chinese_numeral(m.group(2))


def _match_solution(sol_blocks: list[QBlock], blk: QBlock) -> QBlock | None:
    for sb in sol_blocks:
        if sb.label == blk.label:
            return sb
    if blk.number is not None and "." not in blk.label:
        for sb in sol_blocks:
            if sb.number == blk.number and "." not in sb.label:
                return sb
    return None


def extract_questions(sources: list[Source], chapters: list[Chapter]) -> list[QuestionItem]:
    q_sources = [s for s in sources if s.kind in QUESTION_KINDS and not s.error and s.pages]
    solutions: dict[str, Source] = {}
    for s in q_sources:
        if s.kind == "solution":
            solutions.setdefault(pair_key(s.rel), s)
    paired: set[str] = set()
    items: list[QuestionItem] = []

    for src in q_sources:
        sol = solutions.get(pair_key(src.rel)) if src.kind != "solution" else None
        sol_blocks = blocks(sol) if sol is not None else []
        if sol is not None:
            paired.add(sol.rel)
        for blk in blocks(src):
            text = "\n".join(blk.lines).strip()
            q, a = _split_answer(text)
            sol_ref: QuestionRef | None = None
            sb = _match_solution(sol_blocks, blk) if sol is not None and a is None else None
            if sb is not None and sol is not None:
                stext = "\n".join(sb.lines).strip()
                a = _strip_shared_lines(stext, q)
                if a is None:
                    _, a = _split_answer(stext)
                sol_ref = QuestionRef(sol.rel, sb.page, sb.head)
            if len(q) < 8 and not a:
                continue
            options, q_body = _options(q)
            m = _POINTS.search(q_body)
            items.append(
                QuestionItem(
                    number=blk.number,
                    label=blk.label,
                    question=q_body,
                    options=options,
                    answer=a,
                    type=question_type(q_body, options),
                    points=int(m.group(1) or m.group(2)) if m else None,
                    source=QuestionRef(src.rel, blk.page, blk.head, src.kind),
                    answer_source=sol_ref or (QuestionRef(src.rel, blk.page, blk.head) if a else None),
                    chapter=_chapter_hint(q_body),
                )
            )

    # 有配对题面的答案文件不能再产生重复题目
    items = [it for it in items if not (it.source.kind == "solution" and it.source.file in paired)]

    assign_chapters(items, chapters)
    for i, it in enumerate(items, 1):
        it.id = f"q{i:03d}"
    return items


def assign_chapters(items: list[QuestionItem], chapters: list[Chapter]) -> None:
    """章节归属：题目里写明的章号 → 教材题号首段 → BM25 推测（标记 chapter_guessed）。"""
    if not chapters:
        return
    numbers = {c.number for c in chapters}
    bm = BM25([tokenize(ch.text) for ch in chapters])
    for it in items:
        if it.chapter in numbers:
            it.chapter_guessed = False
            continue
        first = it.label.split(".")[0] if "." in it.label else None
        if first and first.isdigit() and int(first) in numbers:
            it.chapter = int(first)  # 教材题号 7.3.6 → 第 7 章
            it.chapter_guessed = False
            continue
        toks = tokenize(" ".join([it.question, " ".join(it.options), it.answer or ""]))
        best = bm.best(toks, 1)
        it.chapter = chapters[best[0][0]].number if best else None
        it.chapter_guessed = True
