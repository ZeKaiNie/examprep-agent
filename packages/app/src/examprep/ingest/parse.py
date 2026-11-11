"""单个文件的完整解析：文本 + 配图 + PDF 布局。在进程池里运行，输入输出都可 pickle（R5.1）。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from .extract import Page, extract_source
from .figures import FigureRecord, PdfPageInfo, file_figures


@dataclass(slots=True)
class ParseRequest:
    root: str  # 资料目录（绝对路径）
    rel: str  # 相对路径（/ 分隔）
    figure_dir: str  # 本文件配图的输出目录（绝对路径，会被清空）
    figure_prefix: str  # 写进记录的路径前缀（相对课程数据目录）


@dataclass(slots=True)
class ParsedFile:
    rel: str
    kind: str
    pages: list[Page] = field(default_factory=list[Page])
    empty_pages: list[int] = field(default_factory=list[int])
    warnings: list[str] = field(default_factory=list[str])
    error: str | None = None
    figures: list[FigureRecord] = field(default_factory=list[FigureRecord])
    layouts: dict[int, dict[str, Any]] = field(default_factory=dict[int, dict[str, Any]])
    seconds: float = 0.0


def layout_to_dict(info: PdfPageInfo) -> dict[str, Any]:
    return {
        "w": info.width,
        "h": info.height,
        "scan": info.scan,
        "figures": [list(b) for b in info.figures],
        "text_boxes": [list(t) for t in info.text_boxes],
    }


def layout_from_dict(n: int, d: dict[str, Any]) -> PdfPageInfo:
    figs: list[Any] = d.get("figures") or []
    boxes: list[Any] = d.get("text_boxes") or []
    return PdfPageInfo(
        number=n,
        width=float(d["w"]),
        height=float(d["h"]),
        scan=bool(d.get("scan")),
        figures=[(float(b[0]), float(b[1]), float(b[2]), float(b[3])) for b in figs],
        text_boxes=[(float(t[0]), float(t[1]), float(t[2]), float(t[3]), str(t[4])) for t in boxes],
    )


def parse_file(req: ParseRequest) -> ParsedFile:
    """解析失败只记录在 ``error`` / ``warnings`` 里，不抛出。"""
    t0 = time.perf_counter()
    path = os.path.join(req.root, req.rel)
    src = extract_source(req.root, path)
    out = ParsedFile(
        rel=req.rel,
        kind=src.kind,
        pages=src.pages,
        empty_pages=src.empty_pages,
        warnings=src.warnings,
        error=src.error,
    )
    if src.error is None and src.kind != "figure":
        try:
            out.figures, infos = file_figures(path, req.rel, req.figure_dir, req.figure_prefix)
        except Exception as exc:  # 一个文件的配图失败不影响导入
            out.warnings.append(f"figures_failed: {exc}")
            infos = {}
        scans = {n for n, info in infos.items() if info.scan}
        for p in out.pages:
            p.scan = p.number in scans
        out.layouts = {n: layout_to_dict(info) for n, info in infos.items()}
    out.seconds = time.perf_counter() - t0
    return out
