"""把抽取后的文件组织成有序的章节。移植自 Skill v5.3 chapters.py。

规则（刻意保持很少）：
1. 文件名或首页前几行带编号的教学文件，一个文件就是一章（lec3.pdf、第3章.pptx、03-sorting.docx、首页 “Lecture 3: ...”）。
2. 没有编号的教学文件，如果内部出现至少两个递增的 ``Chapter N`` / ``第N章`` / ``Lecture N`` 标题，就按标题拆分。
3. 仍然没有编号的文件，按文件顺序排在有编号的章节之后。编号相同的文件合并成一章。
作业、试卷、答案文件不构成章节，只进题库。
"""

from __future__ import annotations

import itertools
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .extract import Source
from .text import chinese_numeral

TEACHING_KINDS = ("lecture", "notes", "other")

# (文件相对路径, 页码, 文本)
Block = tuple[str, int, str]

_EN_WORDS = r"(?:chapter|chap|lecture|lec|unit|week|module|topic|lesson|section|part)"
_HEAD_EN = re.compile(rf"^\s*#{{0,6}}\s*{_EN_WORDS}\s*[.#]?\s*(\d{{1,3}})\b[\s:.\-–—]*(.*?)\s*$", re.I)
_HEAD_ZH = re.compile(
    r"^\s*#{0,6}\s*第\s*([0-9一二三四五六七八九十百]+)\s*(?:章|讲|单元|课|节|部分)\s*[:：.\-\s]*(.*?)\s*$"
)
_ANY_EN = re.compile(rf"{_EN_WORDS}\s*[.#]?\s*(\d{{1,3}})(?!\d)[\s:.\-–—]*(.*?)\s*$", re.I)
_ANY_ZH = re.compile(
    r"第\s*([0-9一二三四五六七八九十百]+)\s*(?:章|讲|单元|课|节|部分)\s*[:：.\-\s]*(.*?)\s*$"
)
_FILE_NUM = re.compile(
    r"(?:^|[^a-z])(?:lec|lecture|ch|chap|chapter|unit|week|module|topic|lesson|l|w|c)[ _\-]?0*(\d{1,3})(?!\d)",
    re.I,
)
_FILE_ZH = re.compile(r"第\s*([0-9一二三四五六七八九十百]+)\s*(?:章|讲|单元|课|节|部分)")
_FILE_LEAD = re.compile(r"^\s*0*(\d{1,3})(?!\d)\s*[ _\-\.、．]")
_MD_HEAD = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$")
_ANY_NUM = re.compile(r"(?:^|[^0-9])0*(\d{1,3})(?!\d)")


@dataclass(slots=True)
class Chapter:
    number: int
    title: str
    blocks: list[Block] = field(default_factory=list[Block])
    sources: list[str] = field(default_factory=list[str])  # 来源文件，按出现顺序
    figures: list[str] = field(default_factory=list[str])  # 按文件名编号挂到本章的图片文件

    @property
    def text(self) -> str:
        return "\n\n".join(b[2] for b in self.blocks if b[2])

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.number,
            "title": self.title,
            "sources": self.sources,
            "figures": self.figures,
            "blocks": [{"file": b[0], "page": b[1], "text": b[2]} for b in self.blocks],
        }


def number_from_name(rel: str, loose: bool = False) -> int | None:
    """文件名里隐含的章号；``loose`` 时也接受裸数字（图片文件）。"""
    name = os.path.splitext(os.path.basename(rel))[0]
    m = _FILE_ZH.search(name)
    if m:
        return chinese_numeral(m.group(1))
    m = _FILE_NUM.search(name)
    if m:
        return int(m.group(1))
    m = _FILE_LEAD.match(name)
    if m:
        return int(m.group(1))
    if loose:
        m = _ANY_NUM.search(name)
        if m:
            return int(m.group(1))
    return None


def heading_in_line(line: str) -> tuple[int, str] | None:
    """该行像章标题时返回 (章号, 标题)。"""
    if len(line) > 120:
        return None
    for rx, is_zh in ((_HEAD_ZH, True), (_HEAD_EN, False)):
        m = rx.match(line)
        if m:
            n = chinese_numeral(m.group(1)) if is_zh else int(m.group(1))
            if n is not None:
                return n, m.group(2).strip(" -:：.")
    return None


def _title_after(lines: list[str], idx: int) -> str:
    """裸 “Chapter N” 标题下一两行印的标题（幻灯片首页）。"""
    out: list[str] = []
    for line in lines[idx + 1 : idx + 4]:
        s = line.strip()
        if (
            not s
            or len(s) > 80
            or re.match(r"^\s*(section|§|\d+\.\d+|by\b|author|instructor|lecturer|prof)", s, re.I)
        ):
            break
        out.append(s)
    return " ".join(out)


def _number_and_title_from_head(lines: list[str]) -> tuple[int | None, str | None]:
    """在文件前几行的短行里找 “Lecture N: Title”。"""
    for idx, line in enumerate(lines[:15]):
        if len(line) > 140:
            continue
        for rx, is_zh in ((_ANY_ZH, True), (_ANY_EN, False)):
            m = rx.search(line)
            if m:
                n = chinese_numeral(m.group(1)) if is_zh else int(m.group(1))
                if n is not None:
                    return n, m.group(2).strip(" -:：.#") or _title_after(lines, idx)
    return None, None


def guess_title(lines: list[str]) -> str:
    for line in lines[:20]:
        m = _MD_HEAD.match(line)
        if m:
            return m.group(1)
    for line in lines[:10]:
        s = line.strip()
        if 2 <= len(s) <= 60 and not s.endswith((".", "。", ",", "，", ";", "；")) and not s[:1].isdigit():
            return s
    return ""


Segment = tuple[int, str, list[Block]]


def _split_internal(src: Source) -> list[Segment] | None:
    """按文件内部递增的章标题拆分无编号文件。"""
    heads: list[tuple[int, int, str]] = []
    seen_lines: list[tuple[int, int, str]] = []
    for page in src.pages:
        for idx, line in enumerate(page.text.split("\n")):
            hit = heading_in_line(line)
            seen_lines.append((page.number, idx, line))
            if hit:
                heads.append((len(seen_lines) - 1, hit[0], hit[1]))
    if len(heads) < 2:
        return None
    numbers = [h[1] for h in heads]
    rising = sum(1 for a, b in itertools.pairwise(numbers) if b > a)
    if len(set(numbers)) < 2 or rising < (len(numbers) - 1) * 0.6:
        return None
    segments: list[Segment] = []
    bounds = [h[0] for h in heads] + [len(seen_lines)]
    if bounds[0] > 0:  # 第一个标题之前的文字并入第一章
        bounds[0] = 0
    for k, (_start, number, title) in enumerate(heads):
        lo, hi = bounds[k], bounds[k + 1]
        by_page: dict[int, list[str]] = {}
        for pnum, _, line in seen_lines[lo:hi]:
            by_page.setdefault(pnum, []).append(line)
        seg_blocks: list[Block] = [(src.rel, p, "\n".join(ls).strip()) for p, ls in sorted(by_page.items())]
        segments.append((number, title or guess_title([ln for _, _, ln in seen_lines[lo:hi]]), seg_blocks))
    return segments


def build_chapters(sources: list[Source]) -> list[Chapter]:
    numbered: dict[int, Chapter] = {}
    unnumbered: list[tuple[str, str, list[Block]]] = []
    figures = [s for s in sources if s.kind == "figure"]

    def add(number: int | None, title: str, rel: str, blocks: list[Block]) -> None:
        if number is None:
            unnumbered.append((title, rel, blocks))
            return
        ch = numbered.get(number)
        if ch is None:
            ch = numbered[number] = Chapter(number, title)
        elif not ch.title and title:
            ch.title = title
        ch.blocks.extend(blocks)
        if rel not in ch.sources:
            ch.sources.append(rel)

    for src in sources:
        if src.kind not in TEACHING_KINDS or src.error or not src.pages:
            continue
        lines = src.text.split("\n")
        n = number_from_name(src.rel)
        if n is None:
            # 无编号的文件：要么是一章（首页写着 “Lecture 1: Intro”），要么是整本书，按内部章标题拆分
            segments = _split_internal(src)
            if segments:
                for seg_n, seg_title, seg_blocks in segments:
                    add(seg_n, seg_title, src.rel, seg_blocks)
                continue
        first_page = src.pages[0].text.split("\n")
        head_n, head_title = _number_and_title_from_head(first_page)
        if n is None:
            n = head_n
        title = head_title or guess_title(lines)
        add(n, title, src.rel, [(src.rel, p.number, p.text) for p in src.pages])

    chapters = [numbered[k] for k in sorted(numbered)]
    next_n = (max(numbered) + 1) if numbered else 1
    for title, rel, blocks in unnumbered:
        ch = Chapter(next_n, title or os.path.splitext(os.path.basename(rel))[0])
        ch.blocks = blocks
        ch.sources = [rel]
        chapters.append(ch)
        next_n += 1

    by_number = {c.number: c for c in chapters}
    for fig in figures:
        num = number_from_name(fig.rel, loose=True)
        target = by_number.get(num) if num is not None else None
        if target is not None:
            target.figures.append(fig.rel)
    return chapters
