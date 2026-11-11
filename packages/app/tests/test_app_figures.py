"""移植自 Skill tests/test_figures.py，并补充题目裁图与图区检测。"""

from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

from helpers import make_pdf

from examprep.ingest import figures


def read_png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)


def test_cluster_merges_touching_boxes() -> None:
    boxes = [
        (0.0, 0.0, 10.0, 10.0),
        (12.0, 0.0, 20.0, 10.0),
        (100.0, 100.0, 110.0, 110.0),
        (5.0, 5.0, 5.0, 9.0),
    ]
    assert sorted(figures.cluster(boxes, gap=5)) == [(0, 0, 20, 10), (100, 100, 110, 110)]


def test_png_bytes_roundtrip_rgb_and_rgba() -> None:
    w, h = 3, 2
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255] * 2)
    data = figures.png_bytes(w, h, 3, w * 3, rgb)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (w, h)
    idat = data.index(b"IDAT") + 4
    length = struct.unpack(">I", data[idat - 8 : idat - 4])[0]
    assert zlib.decompress(data[idat : idat + length]) == b"\x00" + rgb[:9] + b"\x00" + rgb[9:]
    rgba = figures.png_bytes(1, 1, 4, 4, b"\x00\x00\x00\xff")
    assert rgba[25] == 6  # color type RGBA


def test_pptx_pictures_are_copied_per_slide(tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    png = figures.png_bytes(1, 1, 3, 3, b"\x00\x00\x00")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", '<p:sld><p:pic><a:blip r:embed="rId2"/></p:pic></p:sld>')
        zf.writestr(
            "ppt/slides/_rels/slide1.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId2" Target="../media/image1.png"/></Relationships>',
        )
        zf.writestr("ppt/media/image1.png", png)
        zf.writestr("ppt/slides/slide2.xml", "<p:sld/>")
    out = tmp_path / "figs"
    recs, infos = figures.file_figures(str(path), "deck.pptx", str(out), "figures/abc")
    assert [(r.page, r.path, r.file) for r in recs] == [(1, "figures/abc/deck_p1_1.png", "deck.pptx")]
    assert (out / "deck_p1_1.png").exists()
    assert infos == {}


def test_docx_without_rels_and_other_ext(tmp_path: Path) -> None:
    path = tmp_path / "n.docx"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
    assert figures.file_figures(str(path), "n.docx", str(tmp_path / "o"), "p") == ([], {})
    txt = tmp_path / "a.md"
    txt.write_text("x", encoding="utf-8")
    assert figures.file_figures(str(txt), "a.md", str(tmp_path / "o2"), "p") == ([], {})


def test_render_page_and_region(tmp_path: Path) -> None:
    pdf = tmp_path / "l.pdf"
    make_pdf(pdf, ["Lecture 1: Intro"])
    out = tmp_path / "page.png"
    assert figures.render_region(str(pdf), 1, None, str(out), scale=1.0) == (612, 792)
    assert read_png_size(out) == (612, 792)
    w, h = figures.render_region(str(pdf), 1, (100, 600, 300, 750), str(out), scale=1.0)
    assert (w, h) == (200 + 2 * figures.MARGIN, 150 + 2 * figures.MARGIN)


def test_text_only_pdf_has_no_figures_and_no_scan(tmp_path: Path) -> None:
    pdf = tmp_path / "l.pdf"
    make_pdf(pdf, ["Only text here", "Second page"])
    infos = figures.analyze_pdf(str(pdf))
    assert sorted(infos) == [1, 2]
    assert infos[1].figures == []
    assert not infos[1].scan
    assert figures.find_text_top(infos[2], "Second page") is not None
    assert figures.find_text_top(infos[2], "absent") is None


def test_drawn_rectangle_is_detected_and_cropped(tmp_path: Path) -> None:
    pdf = tmp_path / "lec1.pdf"
    make_pdf(
        pdf, ["Problem 1. Look at the figure", "Problem 2. Plain text"], rects={0: [(100, 300, 200, 150)]}
    )
    out = tmp_path / "figs"
    recs, infos = figures.file_figures(str(pdf), "lec1.pdf", str(out), "figures/x")
    assert len(recs) == 1
    assert recs[0].page == 1
    assert recs[0].box == [100, 300, 300, 450]
    assert (out / "lec1_p1_1.png").exists()
    # 题目裁图：题 1 所在区域与图重叠才裁，题 2 是纯文字
    heads = [(1, "Problem 1. Look at the figure", "1"), (2, "Problem 2. Plain text", "2")]
    seg1 = figures.segments_for(infos, heads, 0)
    assert seg1[0][0] == 1
    paths = figures.crop_blocks(str(pdf), infos, seg1, str(out), "crops", "lec1", "q001")
    assert paths == ["crops/lec1_q001_1.png"]
    seg2 = figures.segments_for(infos, heads, 1)
    assert figures.crop_blocks(str(pdf), infos, seg2, str(out), "crops", "lec1", "q002") == []
    assert figures.segments_for(infos, [(5, "x", "1")], 0) == []


def test_repeated_decoration_is_ignored(tmp_path: Path) -> None:
    pdf = tmp_path / "deck.pdf"
    logo = (20.0, 20.0, 80.0, 50.0)
    make_pdf(pdf, ["a", "b", "c", "d"], rects={i: [logo] for i in range(4)})
    infos = figures.analyze_pdf(str(pdf))
    assert all(info.figures == [] for info in infos.values())


def test_safe_stem() -> None:
    assert figures.safe_stem("dir/第1章 线性表.pdf") == "第1章_线性表"
    assert figures.safe_stem("!!!.pdf") == "file"
