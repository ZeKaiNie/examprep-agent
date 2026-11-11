"""移植自 Skill tests/test_extract.py。"""

from __future__ import annotations

from pathlib import Path

from helpers import make_docx, make_pdf, make_pptx

from examprep.ingest import extract
from examprep.ingest.extract import Page


def test_text_form_feed_pages_and_gbk(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes("第一页 内容\f第二页".encode("gb18030"))
    pages = extract.read_text(str(tmp_path / "a.txt"))
    assert [p.number for p in pages] == [1, 2]
    assert pages[1].text == "第二页"
    assert extract.decode_text(b"\xff\xfe\x00") == "\xff\xfe\x00"  # 最后退回 latin-1


def test_docx_headings_pagebreak_table(tmp_path: Path) -> None:
    make_docx(
        tmp_path / "n.docx",
        [
            ("h1", "Chapter 1 Intro"),
            "Hello world",
            "PAGEBREAK",
            "Second page",
            ("table", [["a", "b"], ["c", "d"]]),
        ],
    )
    pages = extract.read_docx(str(tmp_path / "n.docx"))
    assert len(pages) == 2
    assert "# Chapter 1 Intro" in pages[0].text
    assert "Hello world" in pages[0].text
    assert "a | b" in pages[1].text


def test_pptx_slides_and_notes(tmp_path: Path) -> None:
    make_pptx(tmp_path / "s.pptx", [["Title", "point one"], ["Slide two"]], notes={2: "speaker note"})
    pages = extract.read_pptx(str(tmp_path / "s.pptx"))
    assert len(pages) == 2
    assert pages[0].text == "Title\npoint one"
    assert "[notes] speaker note" in pages[1].text


def test_html(tmp_path: Path) -> None:
    (tmp_path / "p.html").write_text(
        "<html><head><style>x{}</style></head><body><h2>Topic</h2><p>Body &amp; text</p><script>1</script></body></html>",
        encoding="utf-8",
    )
    pages = extract.read_html(str(tmp_path / "p.html"))
    assert "## Topic" in pages[0].text
    assert "Body & text" in pages[0].text
    assert "x{}" not in pages[0].text


def test_pdf(tmp_path: Path) -> None:
    make_pdf(tmp_path / "l.pdf", ["Lecture 2: Sorting", "Merge sort is stable"])
    pages = extract.read_pdf(str(tmp_path / "l.pdf"))
    assert len(pages) == 2
    assert "Lecture 2: Sorting" in pages[0].text
    assert "Merge sort" in pages[1].text


def test_broken_pdf_is_reported_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "lec1.pdf").write_bytes(b"not a pdf")
    src = extract.extract_source(str(tmp_path), str(tmp_path / "lec1.pdf"))
    assert src.error is not None
    assert src.pages == []


def test_classify() -> None:
    assert extract.classify("slides/lec3.pdf") == "lecture"
    assert extract.classify("hw/hw2.pdf") == "homework"
    assert extract.classify("hw2_solutions.pdf") == "solution"
    assert extract.classify("2024 期末试卷.docx") == "exam"
    assert extract.classify("作业3答案.txt") == "solution"
    assert extract.classify("MIT6_006S20_q1.pdf") == "exam"
    assert extract.classify("我的笔记.md") == "notes"
    assert extract.classify("random.txt", "Problem 1. Prove that") == "homework"
    assert extract.classify("random.txt", "Definitions") == "lecture"


def test_strip_repeated_headers() -> None:
    words = ["alpha", "beta", "gamma", "delta"]
    pages = [
        Page(i, f"{i} 6.006 Quiz 1 Name\n{words[i - 1]} body\nStep 1\n{words[i - 1]} tail\n{i}")
        for i in range(1, 5)
    ]
    out = extract.strip_repeated_lines(pages)
    # 页眉和页码去掉；页中间重复的行（“Step 1”）保留
    assert out[0].text == "alpha body\nStep 1\nalpha tail"
    assert out[3].text == "delta body\nStep 1\ndelta tail"
    assert extract.strip_repeated_lines(pages[:2]) == pages[:2]


def test_scan_skips_workspace_hidden_and_readme(tmp_path: Path) -> None:
    (tmp_path / "exam-cram").mkdir()
    (tmp_path / "sub").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "exam-cram" / "x.md").write_text("ignored", encoding="utf-8")
    (tmp_path / ".hidden" / "y.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "sub" / "lec1.md").write_text("# Lecture 1\ncontent", encoding="utf-8")
    (tmp_path / "fig2.png").write_bytes(b"\x89PNG")
    (tmp_path / "README.md").write_text("about this folder", encoding="utf-8")
    (tmp_path / "~$lock.docx").write_bytes(b"")
    (tmp_path / "data.csv").write_text("a,b", encoding="utf-8")
    srcs = extract.scan(str(tmp_path))
    assert [s.rel for s in srcs] == ["fig2.png", "sub/lec1.md"]
    assert srcs[0].kind == "figure"
    assert srcs[0].is_figure
    assert srcs[1].kind == "lecture"


def test_empty_pages_and_warnings(tmp_path: Path) -> None:
    (tmp_path / "lec1.txt").write_text("short\f" + "real content " * 5, encoding="utf-8")
    src = extract.extract_source(str(tmp_path), str(tmp_path / "lec1.txt"))
    assert src.empty_pages == [1]
    assert src.warnings == ["some_pages_without_text"]
    (tmp_path / "lec2.txt").write_text("x", encoding="utf-8")
    src2 = extract.extract_source(str(tmp_path), str(tmp_path / "lec2.txt"))
    assert src2.warnings == ["no_text"]
    assert Page(1, "a").to_dict() == {"n": 1, "text": "a", "scan": False}
