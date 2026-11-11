"""把课程文件变成按页的纯文本。移植自 Skill v5.3 extract.py。

支持：PDF（pypdfium2）、DOCX、PPTX（标准库 zip + xml）、Markdown / TXT / HTML。图片只登记为 figure，不解析。
每页保留页码，讲解时才能引用“文件 p.N”。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import html as _html
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

from .text import clean

TEXT_EXT = {".md", ".markdown", ".txt", ".text", ".rst"}
HTML_EXT = {".html", ".htm"}
DOC_EXT = {".pdf", ".docx", ".pptx"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
SUPPORTED_EXT = TEXT_EXT | HTML_EXT | DOC_EXT | IMAGE_EXT
SKIP_DIRS = {"exam-cram", ".git", "__pycache__", "node_modules", ".ipynb_checkpoints"}
SKIP_NAMES = {"README", "SOURCE", "SOURCES", "LICENSE", "CHANGELOG", "CONTRIBUTING"}
MAX_FILE_BYTES = 200 * 1024 * 1024
EMPTY_PAGE_CHARS = 20

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

KIND_PATTERNS = [
    ("solution", r"sol(?:ution)?s?\b|answer|answers|key\b|答案|解答|参考答案|题解"),
    (
        "exam",
        r"exam|quiz|test\b|midterm|final|paper|(?:^|[^a-z])q\d+|试卷|真题|期中|期末|测验|模拟|考试|小测|试题",
    ),
    ("homework", r"hw\d*|homework|pset|ps\d+|problem|assignment|exercise|作业|习题|练习|题目"),
    ("notes", r"notes?\b|summary|cheat|review|笔记|总结|重点|复习|提纲|纲要"),
    (
        "lecture",
        r"lec(?:ture)?|slides?|chapter|ch\d|ppt|课件|讲义|第.{1,4}[章讲节]|教材|课本|textbook|unit|week",
    ),
]


@dataclass(slots=True)
class Page:
    number: int
    text: str
    scan: bool = False  # 整页被图片覆盖：多半是扫描件或手写

    def to_dict(self) -> dict[str, Any]:
        return {"n": self.number, "text": self.text, "scan": self.scan}


@dataclass(slots=True)
class Source:
    """抽取后的一个课程文件。"""

    path: str
    rel: str
    kind: str
    pages: list[Page] = field(default_factory=list[Page])
    warnings: list[str] = field(default_factory=list[str])
    empty_pages: list[int] = field(default_factory=list[int])  # 无文字的页（扫描、纯图）
    error: str | None = None

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def is_figure(self) -> bool:
        return self.kind == "figure"


# ---------------------------------------------------------------- 各格式读取


def read_pdf(path: str) -> list[Page]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        pages: list[Page] = []
        for i in range(len(pdf)):
            try:
                txt = str(pdf[i].get_textpage().get_text_range())
            except Exception:
                txt = ""
            pages.append(Page(i + 1, clean(txt)))
        return pages
    finally:
        pdf.close()


def _docx_paragraph_text(p: ET.Element) -> str:
    parts: list[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == _W + "t":
            parts.append(node.text or "")
        elif tag == _W + "tab":
            parts.append("\t")
        elif tag == _W + "br" and node.get(_W + "type") != "page":
            parts.append("\n")
    return "".join(parts)


def _docx_has_page_break(p: ET.Element) -> bool:
    for node in p.iter():
        if node.tag == _W + "br" and node.get(_W + "type") == "page":
            return True
        if node.tag == _W + "lastRenderedPageBreak":
            return True
    return False


def read_docx(path: str) -> list[Page]:
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find(_W + "body")
    pages: list[Page] = []
    cur: list[str] = []
    if body is None:
        return pages

    def flush() -> None:
        pages.append(Page(len(pages) + 1, clean("\n".join(cur))))
        cur.clear()

    for el in body:
        if el.tag == _W + "p":
            if _docx_has_page_break(el) and cur:
                flush()
            style = el.find(_W + "pPr/" + _W + "pStyle")
            txt = _docx_paragraph_text(el)
            if style is not None:
                m = re.search(r"(?:heading|标题)\s*([1-6])", style.get(_W + "val", ""), re.I)
                if m and txt.strip():
                    txt = "#" * int(m.group(1)) + " " + txt.strip()
            cur.append(txt)
        elif el.tag == _W + "tbl":
            for tr in el.iter(_W + "tr"):
                cells = [
                    "".join(_docx_paragraph_text(p) for p in tc.iter(_W + "p")).strip()
                    for tc in tr.findall(_W + "tc")
                ]
                cur.append(" | ".join(cells))
            cur.append("")
    if cur:
        flush()
    return pages


def _pptx_text(root: ET.Element) -> str:
    lines: list[str] = []
    for para in root.iter(_A + "p"):
        runs = [t.text or "" for t in para.iter(_A + "t")]
        line = "".join(runs).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def slide_number(name: str) -> int:
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


def read_pptx(path: str) -> list[Page]:
    pages: list[Page] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=slide_number)
        for i, name in enumerate(slides, 1):
            text = _pptx_text(ET.fromstring(zf.read(name)))
            notes = f"ppt/notesSlides/notesSlide{slide_number(name)}.xml"
            if notes in names:
                ntext = _pptx_text(ET.fromstring(zf.read(notes)))
                ntext = re.sub(r"^\d+$", "", ntext, flags=re.M).strip()  # 幻灯片编号占位符
                if ntext:
                    text += "\n\n[notes] " + ntext
            pages.append(Page(i, clean(text)))
    return pages


def decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_text(path: str) -> list[Page]:
    with open(path, "rb") as fh:
        text = decode_text(fh.read())
    if "\f" in text:
        return [Page(i, clean(t)) for i, t in enumerate(text.split("\f"), 1)]
    return [Page(1, clean(text))]


def read_html(path: str) -> list[Page]:
    text = read_text(path)[0].text
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<h([1-6])[^>]*>", lambda m: "\n\n" + "#" * int(m.group(1)) + " ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6]|br)\s*>|<br\s*/?>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return [Page(1, clean(_html.unescape(text)))]


READERS: dict[str, Any] = {".pdf": read_pdf, ".docx": read_docx, ".pptx": read_pptx}
for _e in TEXT_EXT:
    READERS[_e] = read_text
for _e in HTML_EXT:
    READERS[_e] = read_html


# ---------------------------------------------------------------- 清理

_DIGIT_EDGE = re.compile(r"^\s*\d+\s*|\s*\d+\s*$")
_EDGE_LINES = 2  # 只有每页开头、结尾的几行可能是页眉页脚


def strip_repeated_lines(pages: list[Page]) -> list[Page]:
    """去掉页眉页脚：在很多页的边缘重复出现的短行（忽略页码差异）。"""
    if len(pages) < 3:
        return pages

    def edges(lines: list[str]) -> set[str]:
        return set(lines[:_EDGE_LINES] + lines[-_EDGE_LINES:])

    seen: dict[str, int] = {}
    for p in pages:
        for line in edges(p.text.split("\n")):
            key = _DIGIT_EDGE.sub("", line).strip()
            if 0 < len(key) <= 80:
                seen[key] = seen.get(key, 0) + 1
    threshold = max(3, len(pages) // 2)
    repeated = {k for k, n in seen.items() if n >= threshold}
    out: list[Page] = []
    for p in pages:
        lines = p.text.split("\n")
        edge = edges(lines)
        kept: list[str] = []
        for line in lines:
            key = _DIGIT_EDGE.sub("", line).strip()
            if line in edge and (key in repeated or re.fullmatch(r"\s*\d{1,4}\s*", line)):
                continue
            kept.append(line)
        out.append(Page(p.number, clean("\n".join(kept)), p.scan))
    return out


# ---------------------------------------------------------------- 扫描与分类


def classify(rel: str, sample: str = "") -> str:
    """先看路径，再看正文开头，判断文件类型。"""
    name = rel.replace("\\", "/").lower()
    for kind, pat in KIND_PATTERNS:
        if re.search(pat, name):
            return kind
    head = sample[:3000]
    if re.search(r"^\s*(problem|question|exercise|题目|第\s*\S+\s*题)\b", head, re.I | re.M):
        return "homework"
    return "lecture"


def natural_key(s: str) -> list[int | str]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_files(materials: str) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(materials):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in files:
            if f.startswith((".", "~$")) or os.path.splitext(f)[0].upper() in SKIP_NAMES:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXT:
                out.append(os.path.join(root, f))
    return sorted(out, key=lambda p: natural_key(os.path.relpath(p, materials)))


def rel_path(materials: str, path: str) -> str:
    return os.path.relpath(path, materials).replace("\\", "/")


def extract_source(materials: str, path: str) -> Source:
    rel = rel_path(materials, path)
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return Source(path, rel, "figure")
    src = Source(path, rel, "other")
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise ValueError(f"file larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        src.pages = READERS[ext](path)
    except Exception as exc:
        src.error = f"{type(exc).__name__}: {exc}"
        return src
    src.empty_pages = [p.number for p in src.pages if len(p.text.strip()) < EMPTY_PAGE_CHARS]
    src.pages = strip_repeated_lines(src.pages)
    src.kind = classify(rel, src.text)
    if src.pages and len(src.empty_pages) == len(src.pages):
        src.warnings.append("no_text")
    elif src.empty_pages:
        src.warnings.append("some_pages_without_text")
    return src


def scan(materials: str) -> list[Source]:
    return [extract_source(materials, p) for p in list_files(materials)]
