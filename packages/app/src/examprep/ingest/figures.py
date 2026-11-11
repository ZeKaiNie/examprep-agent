"""从课程文件里找出并裁剪图片，讲解时可以直接展示。移植自 Skill v5.3 figures.py 与 cli.py 的题目裁图逻辑。

PDF：从页面对象列表定位矢量图和内嵌图片，聚类成区域，渲染后存为 PNG。
PPTX / DOCX：内嵌图片直接从 zip 里复制。
扫描页（一张图覆盖大半页）只做标记，不当作配图裁剪。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportAttributeAccessIssue=false

from __future__ import annotations

import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

MIN_W, MIN_H = 48, 36  # pt；更小的是分隔线、项目符号、下划线
MARGIN = 8  # 裁剪时四周留白（pt）
GAP = 10  # 间距小于该值的图元合并成一张图（pt）
SCAN_COVER = 0.6  # 图片面积 / 页面面积超过该值判为扫描页
MAX_PER_PAGE = 4
RENDER_SCALE = 2.0
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}

Box = tuple[float, float, float, float]  # (left, bottom, right, top)，PDF 坐标
TextBox = tuple[float, float, float, float, str]


@dataclass(slots=True)
class PdfPageInfo:
    number: int
    width: float
    height: float
    figures: list[Box] = field(default_factory=list[Box])  # 候选图区
    scan: bool = False
    text_boxes: list[TextBox] = field(default_factory=list[TextBox])


@dataclass(slots=True)
class FigureRecord:
    file: str
    page: int
    path: str  # 相对数据目录的路径
    kind: str = "figure"  # figure / question / answer
    box: list[int] | None = None
    w: int | None = None
    h: int | None = None
    qid: str | None = None


# ---------------------------------------------------------------- 几何


def clip(box: Box, w: float, h: float) -> Box:
    left, bottom, right, top = box
    return (max(0.0, left), max(0.0, bottom), min(w, right), min(h, top))


def area(box: Box) -> float:
    left, bottom, right, top = box
    return max(0.0, right - left) * max(0.0, top - bottom)


def touch(a: Box, b: Box, gap: float) -> bool:
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def cluster(boxes: Sequence[Box], gap: float = GAP) -> list[Box]:
    """反复合并相互接触（间距不超过 gap）的框，直到不再变化。"""
    items: list[Box] = [(b[0], b[1], b[2], b[3]) for b in boxes if area(b) > 0]
    changed = True
    while changed:
        changed = False
        out: list[Box] = []
        for box in items:
            for i, other in enumerate(out):
                if touch(box, other, gap):
                    out[i] = (
                        min(box[0], other[0]),
                        min(box[1], other[1]),
                        max(box[2], other[2]),
                        max(box[3], other[3]),
                    )
                    changed = True
                    break
            else:
                out.append(box)
        items = out
    return items


# ---------------------------------------------------------------- PNG


def png_bytes(width: int, height: int, channels: int, stride: int, buf: bytes) -> bytes:
    """只用 zlib 把 RGB/RGBA 位图（自上而下的行）编码为 PNG。"""
    color_type = 6 if channels == 4 else 2
    raw = bytearray()
    row_len = width * channels
    for y in range(height):
        start = y * stride
        raw.append(0)
        raw += buf[start : start + row_len]

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- PDF 分析


def _page_bounds(obj: Any) -> Box:
    """页面对象在页面坐标系中的边界（Form XObject 里的对象报告的是局部坐标，要逐层变换）。"""
    left, bottom, right, top = obj.get_bounds()
    corners = [(left, bottom), (right, bottom), (left, top), (right, top)]
    cont = obj.container
    while cont is not None:
        a, b, c, d, e, f = cont.get_matrix().get()
        corners = [(a * x + c * y + e, b * x + d * y + f) for x, y in corners]
        cont = cont.container
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def analyze_pdf(path: str) -> dict[int, PdfPageInfo]:
    """返回 {页码: PdfPageInfo}，含图区与扫描标记。"""
    import pypdfium2 as pdfium
    from pypdfium2 import raw as R

    pdf = pdfium.PdfDocument(path)
    try:
        infos: dict[int, PdfPageInfo] = {}
        raw_boxes: dict[int, tuple[list[Box], list[Box]]] = {}
        for i in range(len(pdf)):
            page = pdf[i]
            w, h = page.get_size()
            info = PdfPageInfo(i + 1, float(w), float(h))
            images: list[Box] = []
            drawings: list[Box] = []
            for obj in page.get_objects(max_depth=4):
                try:
                    box = clip(_page_bounds(obj), w, h)
                except Exception:
                    continue
                if obj.type == R.FPDF_PAGEOBJ_IMAGE:
                    images.append(box)
                elif obj.type in (R.FPDF_PAGEOBJ_PATH, R.FPDF_PAGEOBJ_SHADING) and area(box) > 1:
                    drawings.append(box)
            page_area = w * h
            if images and sum(area(b) for b in cluster(images, 2)) >= SCAN_COVER * page_area:
                info.scan = True
            tp = page.get_textpage()
            for k in range(tp.count_rects()):
                left, bottom, right, top = tp.get_rect(k)
                text = tp.get_text_bounded(left=left, bottom=bottom, right=right, top=top)
                info.text_boxes.append((float(left), float(bottom), float(right), float(top), str(text)))
            raw_boxes[i + 1] = (images, drawings)
            infos[i + 1] = info
    finally:
        pdf.close()

    # 装饰：在很多页上出现同一个框（幻灯片模板线条、logo）
    seen: Counter[tuple[int, ...]] = Counter()
    for images, drawings in raw_boxes.values():
        for rounded in {tuple(round(v) for v in b) for b in images + drawings}:
            seen[rounded] += 1
    threshold = max(3, len(infos) * 0.4)
    decor = {box for box, n in seen.items() if n >= threshold}

    for n, info in infos.items():
        if info.scan:
            continue
        images, drawings = raw_boxes[n]
        boxes = [b for b in images + drawings if tuple(round(v) for v in b) not in decor]
        page_area = info.width * info.height
        regions = [
            box
            for box in cluster(boxes)
            if box[2] - box[0] >= MIN_W and box[3] - box[1] >= MIN_H and area(box) < 0.95 * page_area
        ]
        regions.sort(key=area, reverse=True)
        info.figures = regions[:MAX_PER_PAGE]
    return infos


def render_page_region(
    page: Any, box: Box | None, out_path: str, scale: float = RENDER_SCALE
) -> tuple[int, int]:
    """把已打开页面的 ``box``（None 表示整页）渲染成 PNG 文件。"""
    w, h = page.get_size()
    if box is None:
        crop: tuple[float, float, float, float] = (0, 0, 0, 0)
    else:
        left, bottom, right, top = clip(
            (box[0] - MARGIN, box[1] - MARGIN, box[2] + MARGIN, box[3] + MARGIN), w, h
        )
        crop = (left, bottom, w - right, h - top)
    bitmap = page.render(scale=scale, crop=crop, rev_byteorder=True)
    data = png_bytes(bitmap.width, bitmap.height, bitmap.n_channels, bitmap.stride, bytes(bitmap.buffer))
    with open(out_path, "wb") as fh:
        fh.write(data)
    return int(bitmap.width), int(bitmap.height)


def render_region(
    path: str, page_number: int, box: Box | None, out_path: str, scale: float = RENDER_SCALE
) -> tuple[int, int]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        return render_page_region(pdf[page_number - 1], box, out_path, scale)
    finally:
        pdf.close()


def find_text_top(info: PdfPageInfo, needle: str) -> float | None:
    """以 ``needle`` 开头的第一个文字框的上沿 y。

    PDF 文字框可能把题头拆开（“Problem 4.” | “[10 points] Trees”），找不到长前缀时依次试更短的前缀。
    """
    flat = " ".join(needle.split()).lower()
    boxes = [(top, " ".join(text.split()).lower()) for _l, _b, _r, top, text in info.text_boxes]
    for n in (40, 20, 10):
        key = flat[:n]
        if not key:
            continue
        for top, text in boxes:
            if text.startswith(key):
                return top
    return None


# ---------------------------------------------------------------- Office 内嵌图


def _zip_media(zf: zipfile.ZipFile, rels_name: str, xml_name: str) -> list[str]:
    """幻灯片或文档部件按出现顺序引用的媒体文件。"""
    names = zf.namelist()
    if rels_name not in names:
        return []
    rels = {rel.get("Id"): rel.get("Target", "") for rel in ET.fromstring(zf.read(rels_name))}
    order = re.findall(r'r:embed="([^"]+)"', zf.read(xml_name).decode("utf-8", "ignore"))
    base = os.path.dirname(xml_name)
    out: list[str] = []
    for rid in order:
        target = rels.get(rid)
        if not target:
            continue
        full = os.path.normpath(os.path.join(base, target)).replace("\\", "/")
        if os.path.splitext(full)[1].lower() in MEDIA_EXT and full in names:
            out.append(full)
    return out


def _slide_no(name: str) -> int:
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else 0


def extract_office_media(path: str, out_dir: str, prefix: str, stem: str) -> list[FigureRecord]:
    """复制 PPTX（按幻灯片）或 DOCX（记为第 1 页）里的图片。"""
    records: list[FigureRecord] = []
    ext = os.path.splitext(path)[1].lower()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if ext == ".pptx":
            slides = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)), key=_slide_no)
            parts = [(_slide_no(n), n, f"ppt/slides/_rels/{os.path.basename(n)}.rels") for n in slides]
        elif ext == ".docx":
            parts = [(1, "word/document.xml", "word/_rels/document.xml.rels")]
        else:
            return records
        for page, xml_name, rels_name in parts:
            for k, media in enumerate(_zip_media(zf, rels_name, xml_name), 1):
                out_name = f"{stem}_p{page}_{k}{os.path.splitext(media)[1].lower()}"
                with open(os.path.join(out_dir, out_name), "wb") as fh:
                    fh.write(zf.read(media))
                records.append(FigureRecord(file="", page=page, path=f"{prefix}/{out_name}"))
                if k >= MAX_PER_PAGE:
                    break
    return records


# ---------------------------------------------------------------- 单文件驱动


def safe_stem(rel: str) -> str:
    stem = os.path.splitext(os.path.basename(rel))[0]
    return re.sub(r"[^\w\-]+", "_", stem).strip("_")[:40] or "file"


def clear_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for old in os.listdir(path):
        full = os.path.join(path, old)
        if os.path.isfile(full):
            os.remove(full)


def file_figures(
    path: str, rel: str, out_dir: str, prefix: str
) -> tuple[list[FigureRecord], dict[int, PdfPageInfo]]:
    """一个文件的配图：写到 ``out_dir``（先清空），记录里的路径以 ``prefix`` 开头。返回 (记录, PDF 页信息)。"""
    clear_dir(out_dir)
    ext = os.path.splitext(path)[1].lower()
    stem = safe_stem(rel)
    if ext in (".pptx", ".docx"):
        records = extract_office_media(path, out_dir, prefix, stem)
        for r in records:
            r.file = rel
        return records, {}
    if ext != ".pdf":
        return [], {}
    import pypdfium2 as pdfium

    infos = analyze_pdf(path)
    records: list[FigureRecord] = []
    pdf = pdfium.PdfDocument(path)
    try:
        for n, info in infos.items():
            for k, box in enumerate(info.figures, 1):
                name = f"{stem}_p{n}_{k}.png"
                w, h = render_page_region(pdf[n - 1], box, os.path.join(out_dir, name))
                records.append(
                    FigureRecord(
                        file=rel, page=n, path=f"{prefix}/{name}", box=[round(v) for v in box], w=w, h=h
                    )
                )
    finally:
        pdf.close()
    return records, infos


# ---------------------------------------------------------------- 题目裁图

Segment = tuple[int, float, float]  # (页, 上沿 y, 下沿 y)


def segments_for(
    infos: dict[int, PdfPageInfo], heads: Sequence[tuple[int, str, str]], i: int
) -> list[Segment]:
    """第 i 个题块覆盖的页面区域，给定全部题头（按顺序）。"""
    page, head = heads[i][0], heads[i][1]
    info = infos.get(page)
    top = find_text_top(info, head) if info else None
    if info is None or top is None:
        return []
    nxt = heads[i + 1] if i + 1 < len(heads) else None
    if nxt and nxt[0] == page:
        nt = find_text_top(infos[page], nxt[1])
        return [(page, top, nt if nt is not None else 24)]
    segs: list[Segment] = [(page, top, 24)]
    if info.scan:
        return segs  # 扫描题单之后的页是学生自己的答题纸，不属于题目
    last = nxt[0] if nxt else min(page + 3, max(infos))
    for p in range(page + 1, last):
        if p in infos:
            segs.append((p, infos[p].height - 24, 24))
    if nxt and nxt[0] in infos:
        nt = find_text_top(infos[nxt[0]], nxt[1])
        if nt is not None:
            segs.append((nxt[0], infos[nxt[0]].height - 24, nt))
    return segs


def crop_blocks(
    pdf_path: str,
    infos: dict[int, PdfPageInfo],
    segments: Sequence[Segment],
    out_dir: str,
    prefix: str,
    stem: str,
    label: str,
) -> list[str]:
    """裁出题目或答案区域。只有区域与检测到的图重叠才裁（纯文字不需要图）。返回相对路径列表。"""
    paths: list[str] = []
    for k, (page, top, bottom) in enumerate(segments, 1):
        info = infos.get(page)
        if info is None:
            continue
        region: Box = (20.0, max(0.0, bottom), info.width - 20, min(info.height, top))
        if info.scan:
            # 扫描的印刷试卷：整块都是图。只有 OCR 层在题号下方有实际内容才裁（题单后的空白纸不是题）
            inside = [tb for tb in info.text_boxes if tb[1] >= region[1] - 1 and tb[3] <= region[3] + 1]
            if region[3] - region[1] < 60 or len(inside) < 3:
                continue
        elif not any(touch(region, fig, 0) for fig in info.figures):
            continue
        else:
            # 去掉块内最后一行文字或图形下方的空白
            content = [tb[1] for tb in info.text_boxes if tb[1] >= region[1] - 1 and tb[3] <= region[3] + 1]
            content += [fig[1] for fig in info.figures if fig[1] >= region[1] - 1 and fig[3] <= region[3] + 1]
            if content:
                region = (region[0], max(region[1], min(content) - 6), region[2], region[3])
        name = f"{stem}_{label}_{k}.png"
        try:
            render_region(pdf_path, page, region, os.path.join(out_dir, name))
        except Exception:
            continue
        paths.append(f"{prefix}/{name}")
    return paths
