"""引用由系统生成（R4，04 文档第 7 节）。

每轮给 Tutor 的证据块编号为 E1…En，编号到 (文件, 页, 字符区间, 证据 id) 的映射只在服务端。
Tutor 只写 ``[[E3]]``（或 ``[[E1, E3]]``），渲染时替换为“（文件 p.N）”。不存在的编号直接删除并记录，
所以模型写不出假页码。流式输出时缓冲可能未闭合的标记，结果与一次性渲染完全一致。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..knowledge.evidence import EvidenceChunk

_MARK = r"\[\[\s*E\d+(?:\s*[,，、;；]\s*E\d+)*\s*\]\]"
# 连续的多个标记（中间只隔空白）合并成一个引用
_RUN = re.compile(rf"\s*{_MARK}(?:\s*{_MARK})*")
_LABEL = re.compile(r"E\d+")


@dataclass(frozen=True, slots=True)
class Citation:
    label: str
    evidence_id: str
    file: str
    page: int
    char_start: int | None = None
    char_end: int | None = None

    @property
    def display(self) -> str:
        return f"{os.path.basename(self.file)} p.{self.page}"


class CitationMap:
    """一轮对话内的编号表。"""

    def __init__(self, chunks: Iterable[EvidenceChunk] = ()) -> None:
        self._by_label: dict[str, Citation] = {}
        self._by_evidence: dict[str, str] = {}
        self._text: dict[str, str] = {}
        for c in chunks:
            self.add(c)

    def add(self, chunk: EvidenceChunk) -> str:
        existing = self._by_evidence.get(chunk.id)
        if existing is not None:
            return existing
        label = f"E{len(self._by_label) + 1}"
        self._by_label[label] = Citation(
            label, chunk.id, chunk.rel, chunk.page, chunk.char_start, chunk.char_end
        )
        self._by_evidence[chunk.id] = label
        self._text[label] = chunk.text
        return label

    def resolve(self, label: str) -> Citation | None:
        return self._by_label.get(label)

    def text(self, label: str) -> str | None:
        """编号对应的证据原文（Verifier 用）。"""
        return self._text.get(label)

    def labels(self) -> list[str]:
        return list(self._by_label)

    def __len__(self) -> int:
        return len(self._by_label)


@dataclass(slots=True)
class Rendered:
    text: str
    citations: list[Citation] = field(default_factory=list[Citation])  # 按首次出现顺序，去重
    unknown: list[str] = field(default_factory=list[str])  # 被删除的编号


class Renderer:
    """有状态的渲染器：多次调用累计引用与未知编号（流式渲染用）。"""

    def __init__(self, cmap: CitationMap) -> None:
        self.cmap = cmap
        self.citations: list[Citation] = []
        self.unknown: list[str] = []

    def _replace(self, m: re.Match[str]) -> str:
        found: list[Citation] = []
        for label in _LABEL.findall(m.group(0)):
            c = self.cmap.resolve(label)
            if c is None:
                self.unknown.append(label)
                continue
            if c not in found:
                found.append(c)
            if c not in self.citations:
                self.citations.append(c)
        if not found:
            return ""
        shown: list[str] = []
        for c in found:
            if c.display not in shown:
                shown.append(c.display)
        return "（" + "；".join(shown) + "）"

    def render(self, text: str) -> str:
        return _RUN.sub(self._replace, text)


def render(text: str, cmap: CitationMap) -> Rendered:
    r = Renderer(cmap)
    out = r.render(text)
    return Rendered(out, r.citations, r.unknown)


class CitationStream:
    """流式渲染：``feed`` 返回可以安全输出的已渲染文本，``flush`` 输出剩余部分。"""

    def __init__(self, cmap: CitationMap) -> None:
        self._r = Renderer(cmap)
        self._buf = ""

    def feed(self, delta: str) -> str:
        self._buf += delta
        cut = _safe_cut(self._buf)
        ready, self._buf = self._buf[:cut], self._buf[cut:]
        return self._r.render(ready)

    def flush(self) -> str:
        ready, self._buf = self._buf, ""
        return self._r.render(ready)

    def result(self, full_text: str) -> Rendered:
        """流结束后的汇总（full_text 为已经输出的完整渲染文本）。"""
        return Rendered(full_text, self._r.citations, self._r.unknown)


_TRAILING_RUN = re.compile(rf"\s*{_MARK}(?:\s*{_MARK})*\s*$")


def _safe_cut(buf: str) -> int:
    """缓冲区里可以安全渲染的前缀长度：保留可能未闭合的标记、结尾的空白和结尾已闭合的标记串。"""
    cut = len(buf)
    open_at = buf.rfind("[[")
    if open_at >= 0 and buf.find("]]", open_at) < 0:
        cut = open_at
    elif buf.endswith("["):
        cut = len(buf) - 1
    head = buf[:cut]
    m = _TRAILING_RUN.search(head)
    if m:
        return m.start()  # 后面可能还有相邻标记，需要一起合并
    stripped = head.rstrip()
    if len(stripped) < len(head):
        return len(stripped)  # 结尾空白可能属于下一个标记串，先留着
    return cut


def strip_markers(text: str) -> str:
    """去掉 ``[[E..]]`` 标记（写入对话历史前用，避免旧编号干扰后续轮次）。"""
    return _RUN.sub("", text)


def has_markers(text: str) -> bool:
    return bool(_RUN.search(text))


def labels_in(text: str) -> list[str]:
    """文本中出现的编号（按出现顺序去重）。"""
    out: list[str] = []
    for m in _RUN.finditer(text):
        for label in _LABEL.findall(m.group(0)):
            if label not in out:
                out.append(label)
    return out


# 一句 = 到句末标点为止（英文句点要求后面是空白、标记或结尾，避免切开 0.5），再带上紧跟的引用标记；
# 没有句末标点的行整体算一句；换行单独成段。三种情况覆盖全部字符，拼接后与原文完全相同。
_SENTENCE = re.compile(
    rf"[^\n]*?(?:[。！？!?；;]|\.(?=\s|\[\[|$))(?:[ \t]*{_MARK}(?:[ \t]*{_MARK})*)?|[^\n]+|\n"
)


def split_sentences(text: str) -> list[str]:
    """按句切分，拼接后与原文完全相同；句末标点后紧跟的引用标记归到这一句。"""
    return _SENTENCE.findall(text)
