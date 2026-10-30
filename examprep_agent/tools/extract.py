# -*- coding: utf-8 -*-
"""Turn course files into pages of plain text.

Supported: PDF (needs `pypdfium2` or `pypdf`, both optional), DOCX, PPTX (stdlib zip+xml),
Markdown / TXT / HTML.  Images are listed as figures, not parsed.
Every page keeps its number so teaching can cite `file p.N`.

Derived from universal-examprep-skill (MIT, (c) ZeKaiNie):
https://github.com/ZeKaiNie/universal-examprep-skill
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
import zipfile
import xml.etree.ElementTree as ET
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

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

KIND_PATTERNS = [
    ("solution", r"sol(?:ution)?s?\b|answer|answers|key\b|答案|解答|参考答案|题解"),
    ("exam", r"exam|quiz|test\b|midterm|final|paper|(?:^|[^a-z])q\d+|试卷|真题|期中|期末|测验|模拟|考试|小测|试题"),
    ("homework", r"hw\d*|homework|pset|ps\d+|problem|assignment|exercise|作业|习题|练习|题目"),
    ("notes", r"notes?\b|summary|cheat|review|笔记|总结|重点|复习|提纲|纲要"),
    ("lecture", r"lec(?:ture)?|slides?|chapter|ch\d|ppt|课件|讲义|第.{1,4}[章讲节]|教材|课本|textbook|unit|week"),
]

logging.getLogger("pypdf").setLevel(logging.ERROR)


class PdfSupportMissing(Exception):
    """Raised when a PDF is encountered but no PDF backend is installed."""


class Page:
    """One page (or slide) of extracted text."""

    __slots__ = ("number", "text", "scan")

    def __init__(self, number: int, text: str, scan: bool = False) -> None:
        self.number = number
        self.text = text
        self.scan = scan  # a picture covers the page: probably a scanned/handwritten sheet

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the chapters.json workspace file."""
        return {"n": self.number, "text": self.text, "scan": self.scan}


class Source:
    """One course file after extraction."""

    def __init__(self, path: str, rel: str, kind: str) -> None:
        self.path = path
        self.rel = rel
        self.kind = kind
        self.pages: list[Page] = []
        self.warnings: list[str] = []
        self.empty_pages: list[int] = []  # page numbers with no text (scans, pure figures)
        self.error: str | None = None

    @property
    def text(self) -> str:
        """All pages joined with blank lines."""
        return "\n\n".join(p.text for p in self.pages if p.text)

    @property
    def is_figure(self) -> bool:
        """True for image files that are listed as figures, not parsed."""
        return self.kind == "figure"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for diagnostics and tests."""
        return {
            "path": self.path,
            "rel": self.rel,
            "kind": self.kind,
            "pages": [p.to_dict() for p in self.pages],
            "empty_pages": self.empty_pages,
            "warnings": self.warnings,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Source:
        """Rebuild a Source from to_dict() output."""
        s = cls(d["path"], d["rel"], d["kind"])
        s.pages = [Page(p["n"], p["text"], p.get("scan", False)) for p in d.get("pages", [])]
        s.empty_pages = d.get("empty_pages", [])
        s.warnings = d.get("warnings", [])
        s.error = d.get("error")
        return s


# ---------------------------------------------------------------- readers

def pdf_backend() -> str | None:
    """Name of the available PDF backend, or None."""
    try:
        import pypdfium2  # noqa: F401
        return "pypdfium2"
    except ImportError:
        pass
    try:
        import pypdf  # noqa: F401
        return "pypdf"
    except ImportError:
        return None


def read_pdf(path: str) -> list[Page]:
    """Extract text page-by-page from a PDF using pypdfium2 or pypdf."""
    backend = pdf_backend()
    if backend is None:
        raise PdfSupportMissing("install pypdfium2 (or pypdf)")
    pages: list[Page] = []
    if backend == "pypdfium2":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        for i in range(len(pdf)):
            try:
                txt = pdf[i].get_textpage().get_text_range()
            except Exception:
                txt = ""
            pages.append(Page(i + 1, clean(txt)))
        return pages
    import pypdf
    reader = pypdf.PdfReader(path)
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        pages.append(Page(i, clean(txt)))
    return pages


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
    """Extract paragraphs, tables and explicit page breaks from a DOCX file."""
    with zipfile.ZipFile(path) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))
    body = root.find(_W + "body")
    pages: list[Page] = []
    cur: list[str] = []
    if body is None:
        return pages

    def flush() -> None:
        pages.append(Page(len(pages) + 1, clean("\n".join(cur))))
        del cur[:]

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
                cells = ["".join(_docx_paragraph_text(p) for p in tc.iter(_W + "p")).strip()
                         for tc in tr.findall(_W + "tc")]
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


def read_pptx(path: str) -> list[Page]:
    """Extract slide text (and speaker notes) from a PPTX file."""
    pages: list[Page] = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        slides = sorted(
            (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)),
        )
        for i, name in enumerate(slides, 1):
            text = _pptx_text(ET.fromstring(zf.read(name)))
            notes = "ppt/notesSlides/notesSlide%d.xml" % int(re.search(r"(\d+)", name).group(1))
            if notes in names:
                ntext = _pptx_text(ET.fromstring(zf.read(notes)))
                ntext = re.sub(r"^\d+$", "", ntext, flags=re.M).strip()  # slide number placeholder
                if ntext:
                    text += "\n\n[notes] " + ntext
            pages.append(Page(i, clean(text)))
    return pages


def read_text(path: str) -> list[Page]:
    """Read a plain-text file, trying UTF-8, GB18030 and Latin-1; form feeds are pages."""
    raw = open(path, "rb").read()
    text = ""
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if "\f" in text:
        return [Page(i, clean(t)) for i, t in enumerate(text.split("\f"), 1)]
    return [Page(1, clean(text))]


def read_html(path: str) -> list[Page]:
    """Strip scripts/styles/tags from an HTML file and keep the readable text."""
    text = read_text(path)[0].text
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<h([1-6])[^>]*>", lambda m: "\n\n" + "#" * int(m.group(1)) + " ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6]|br)\s*>|<br\s*/?>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return [Page(1, clean(_html.unescape(text)))]


READERS = {".pdf": read_pdf, ".docx": read_docx, ".pptx": read_pptx}
for _e in TEXT_EXT:
    READERS[_e] = read_text
for _e in HTML_EXT:
    READERS[_e] = read_html


# ---------------------------------------------------------------- cleanup

_DIGIT_EDGE = re.compile(r"^\s*\d+\s*|\s*\d+\s*$")
_EDGE_LINES = 2  # only the first/last lines of a page can be a running header/footer


def strip_repeated_lines(pages: list[Page]) -> list[Page]:
    """Drop running headers/footers: short edge lines repeated on many pages (ignoring page numbers)."""
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


# ---------------------------------------------------------------- scanning

def classify(rel: str, sample: str = "") -> str:
    """Guess what a file is from its path first, then from its content."""
    name = rel.replace("\\", "/").lower()
    for kind, pat in KIND_PATTERNS:
        if re.search(pat, name):
            return kind
    head = sample[:3000]
    if re.search(r"^\s*(problem|question|exercise|题目|第\s*\S+\s*题)\b", head, re.I | re.M):
        return "homework"
    return "lecture"


def natural_key(s: str) -> list[Any]:
    """Sort key that orders `lec2` before `lec10`."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _excluded_set(exclude_dirs: Any) -> set[str]:
    """Absolute paths of directories that must never be treated as course material."""
    if isinstance(exclude_dirs, (str, os.PathLike)):
        exclude_dirs = [exclude_dirs]
    return {os.path.abspath(os.fspath(d)) for d in (exclude_dirs or ())}


def list_files(materials: str, exclude_dirs: Any = ()) -> list[str]:
    """All supported course files under `materials`, skipping hidden/workspace folders.

    ``exclude_dirs`` prunes the actual study workspace when it lives inside the
    materials folder (``--workspace ./course/custom_ws``); otherwise a repeated
    setup would ingest the workspace's own generated chapters and progress files.
    """
    excluded = _excluded_set(exclude_dirs)
    out: list[str] = []
    for root, dirs, files in os.walk(materials):
        dirs[:] = sorted(
            d for d in dirs
            if d not in SKIP_DIRS and not d.startswith(".")
            and os.path.abspath(os.path.join(root, d)) not in excluded
        )
        for f in files:
            if f.startswith(".") or f.startswith("~$") or os.path.splitext(f)[0].upper() in SKIP_NAMES:
                continue
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXT:
                out.append(os.path.join(root, f))
    return sorted(out, key=lambda p: natural_key(os.path.relpath(p, materials)))


def extract_source(materials: str, path: str) -> Source:
    """Extract one file into a Source, recording read errors instead of raising."""
    rel = os.path.relpath(path, materials).replace("\\", "/")
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        src = Source(path, rel, "figure")
        return src
    src = Source(path, rel, "other")
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise ValueError("file larger than %d MB" % (MAX_FILE_BYTES // (1024 * 1024)))
        src.pages = READERS[ext](path)
    except PdfSupportMissing:
        src.error = "pdf_support_missing"
        return src
    except Exception as exc:
        src.error = "%s: %s" % (type(exc).__name__, exc)
        return src
    src.empty_pages = [p.number for p in src.pages if len(p.text.strip()) < 20]
    src.pages = strip_repeated_lines(src.pages)
    src.kind = classify(rel, src.text)
    if src.pages and len(src.empty_pages) == len(src.pages):
        src.warnings.append("no_text")
    elif src.empty_pages:
        src.warnings.append("some_pages_without_text")
    return src


def scan(materials: str, exclude_dirs: Any = ()) -> list[Source]:
    """Extract every supported file under `materials`, in natural file-name order."""
    return [extract_source(materials, p) for p in list_files(materials, exclude_dirs=exclude_dirs)]
