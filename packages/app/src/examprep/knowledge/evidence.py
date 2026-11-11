"""证据块：可被引用的最小单元（约 600 字），带文件、页、章与字符区间。

证据 id 由来源位置和文本内容决定，同一份资料重复导入 id 不变。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from examprep_llm import estimate_tokens

from ..ingest.chapters import Chapter
from ..ingest.text import pack, tokenize
from .bm25 import BM25

CHUNK_CHARS = 600


@dataclass(slots=True)
class EvidenceChunk:
    id: str
    rel: str
    page: int
    chapter: int
    ordinal: int  # 同一 (文件, 页) 内的序号
    text: str
    char_start: int | None
    char_end: int | None
    tokens: int


def evidence_id(rel: str, page: int, chapter: int, ordinal: int, text: str) -> str:
    digest = hashlib.sha1(f"{rel}|{page}|{chapter}|{ordinal}|{text}".encode()).hexdigest()
    return "E" + digest[:10]


def _squash(text: str) -> tuple[str, list[int]]:
    """把连续空白压成一个空格，并记录每个字符在原文中的下标。"""
    chars: list[str] = []
    index: list[int] = []
    prev_space = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_space:
                continue
            chars.append(" ")
            prev_space = True
        else:
            chars.append(ch)
            prev_space = False
        index.append(i)
    return "".join(chars), index


def locate(piece: str, page_text: str) -> tuple[int | None, int | None]:
    """片段在页文本中的字符区间。打包时长段落会被重新断行，完整匹配失败时按空白归一后再匹配。"""
    if not page_text:
        return None, None
    start = page_text.find(piece)
    if start >= 0:
        return start, start + len(piece)
    norm_page, index = _squash(page_text)
    norm_piece, _ = _squash(piece)
    s = norm_page.find(norm_piece)
    if s < 0 or not norm_piece:
        return None, None
    return index[s], index[s + len(norm_piece) - 1] + 1


def build_evidence(
    chapters: Sequence[Chapter], page_texts: Mapping[tuple[str, int], str], *, chunk_chars: int = CHUNK_CHARS
) -> list[EvidenceChunk]:
    out: list[EvidenceChunk] = []
    ordinals: dict[tuple[str, int], int] = {}
    for ch in chapters:
        for rel, page, text in ch.blocks:
            for piece in pack(text, chunk_chars):
                key = (rel, page)
                ordinal = ordinals.get(key, 0)
                ordinals[key] = ordinal + 1
                start, end = locate(piece, page_texts.get(key, ""))
                out.append(
                    EvidenceChunk(
                        id=evidence_id(rel, page, ch.number, ordinal, piece),
                        rel=rel,
                        page=page,
                        chapter=ch.number,
                        ordinal=ordinal,
                        text=piece,
                        char_start=start,
                        char_end=end,
                        tokens=estimate_tokens(piece),
                    )
                )
    return out


@dataclass(slots=True)
class Hit:
    chunk: EvidenceChunk
    score: float


class EvidenceIndex:
    """BM25 检索；按章过滤时只在该章的块里建索引。"""

    def __init__(self, chunks: Iterable[EvidenceChunk]) -> None:
        self.chunks = list(chunks)
        self._all = BM25([tokenize(c.text) for c in self.chunks])

    def search(self, query: str, k: int = 5, *, chapter: int | None = None) -> list[Hit]:
        """返回按得分排序的命中；空列表表示资料里没有相关内容。"""
        q = tokenize(query)
        if chapter is None:
            return [Hit(self.chunks[i], s) for i, s in self._all.best(q, k)]
        pool = [c for c in self.chunks if c.chapter == chapter]
        if not pool:
            return []
        bm = BM25([tokenize(c.text) for c in pool])
        return [Hit(pool[i], s) for i, s in bm.best(q, k)]
