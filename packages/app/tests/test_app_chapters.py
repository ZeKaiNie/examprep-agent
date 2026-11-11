"""移植自 Skill tests/test_chapters.py。"""

from __future__ import annotations

import pytest

from examprep.ingest.chapters import build_chapters, heading_in_line, number_from_name
from examprep.ingest.extract import Page, Source


def src(rel: str, kind: str, pages: list[str]) -> Source:
    s = Source("/m/" + rel, rel, kind)
    s.pages = [Page(i, t) for i, t in enumerate(pages, 1)]
    return s


@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("MIT6_006S20_lec3.pdf", 3),
        ("Lecture 12 - Graphs.pptx", 12),
        ("ch05_hashing.docx", 5),
        ("第三章_树.md", 3),
        ("第10讲.pdf", 10),
        ("03-sorting.pdf", 3),
        ("week4.pdf", 4),
        ("psyc110_lecture02.md", 2),
        ("march-2024-notes.pdf", None),
        ("intro.pdf", None),
    ],
)
def test_number_from_name(name: str, want: int | None) -> None:
    assert number_from_name(name) == want


def test_number_from_name_loose_for_figures() -> None:
    assert number_from_name("fig2.png") is None
    assert number_from_name("fig2.png", loose=True) == 2


def test_heading_in_line() -> None:
    assert heading_in_line("第二章 线性表") == (2, "线性表")
    assert heading_in_line("## Chapter 3: Trees") == (3, "Trees")
    assert heading_in_line("x" * 130) is None
    assert heading_in_line("plain line") is None


def test_one_file_per_chapter_with_titles_and_merge() -> None:
    sources = [
        src("lec2.pdf", "lecture", ["Instructors: X Lecture 2: Data Structures", "more"]),
        src("lec1.pdf", "lecture", ["Lecture 1: Introduction", "p2"]),
        src("ch2_notes.md", "notes", ["# Extra notes on chapter two"]),
        src("hw1.pdf", "homework", ["Problem 1. x"]),
        src("fig2.png", "figure", []),
    ]
    chs = build_chapters(sources)
    assert [c.number for c in chs] == [1, 2]
    assert chs[0].title == "Introduction"
    assert chs[1].title == "Data Structures"
    assert chs[1].sources == ["lec2.pdf", "ch2_notes.md"]
    assert chs[1].figures == ["fig2.png"]
    assert [b[1] for b in chs[0].blocks] == [1, 2]
    assert chs[0].to_dict()["blocks"][0]["file"] == "lec1.pdf"


def test_split_single_file_on_internal_headings() -> None:
    text = "教材\n\n第一章 绪论\n\n内容一\n\n第二章 线性表\n\n内容二\n\n第三章 栈\n\n内容三"
    chs = build_chapters([src("数据结构教材.md", "lecture", [text])])
    assert [(c.number, c.title) for c in chs] == [(1, "绪论"), (2, "线性表"), (3, "栈")]
    assert "内容二" in chs[1].text
    assert "内容三" not in chs[1].text


def test_numbered_file_is_not_split_on_subsections() -> None:
    text = "# Lecture 2 - Brain\n\n## Chapter 1. Dualism\n\ntext\n\n## Chapter 2. Materialism\n\nmore"
    chs = build_chapters([src("psyc110_lecture02.md", "lecture", [text])])
    assert len(chs) == 1
    assert chs[0].number == 2
    assert chs[0].title == "Brain"


def test_unnumbered_files_appended_in_order() -> None:
    chs = build_chapters(
        [
            src("zeta.md", "lecture", ["# Zeta topic\n\nbody"]),
            src("lec1.md", "lecture", ["# Lecture 1: A\n\nbody"]),
            src("alpha.md", "lecture", ["Alpha Notes\n\nbody body."]),
        ]
    )
    assert [(c.number, c.title) for c in chs] == [(1, "A"), (2, "Zeta topic"), (3, "Alpha Notes")]


def test_falling_headings_do_not_split() -> None:
    text = "第三章 甲\n\n内容\n\n第一章 乙\n\n内容"
    chs = build_chapters([src("book.md", "lecture", [text])])
    assert len(chs) == 1


def test_empty_and_failed_sources_ignored() -> None:
    bad = src("lec9.pdf", "lecture", [])
    bad.error = "broken"
    assert build_chapters([bad]) == []
