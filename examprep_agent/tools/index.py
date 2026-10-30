# -*- coding: utf-8 -*-
"""Tiny BM25 retrieval over chapter chunks. No dependencies, rebuilt in memory on load.

Derived from universal-examprep-skill (MIT, (c) ZeKaiNie):
https://github.com/ZeKaiNie/universal-examprep-skill
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .text import pack, tokenize

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .chapters import Chapter

INDEX_FILE = "index.json"
CHUNK_CHARS = 600
K1, B = 1.5, 0.75


class BM25:
    """Okapi BM25 over an in-memory list of tokenized documents."""

    def __init__(self, docs_tokens: Sequence[Sequence[str]]) -> None:
        self.n = len(docs_tokens)
        self.tf = [Counter(t) for t in docs_tokens]
        self.dl = [len(t) for t in docs_tokens]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 1.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        self.idf = {t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query_tokens: Iterable[str]) -> list[float]:
        """BM25 score of every document for the query tokens."""
        out = [0.0] * self.n
        for q in set(query_tokens):
            idf = self.idf.get(q)
            if idf is None:
                continue
            for i, c in enumerate(self.tf):
                f = c.get(q)
                if not f:
                    continue
                out[i] += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * self.dl[i] / self.avgdl))
        return out

    def best(self, query_tokens: Iterable[str], k: int = 5) -> list[tuple[int, float]]:
        """Top-k (document index, score) pairs with a positive score."""
        sc = self.scores(query_tokens)
        order = sorted(range(self.n), key=lambda i: -sc[i])
        return [(i, sc[i]) for i in order[:k] if sc[i] > 0]


def chunks_for_chapter(ch: "Chapter") -> list[dict[str, Any]]:
    """BM25-sized chunks of one chapter, carrying file/page provenance."""
    out: list[dict[str, Any]] = []
    for rel, page, text in ch.blocks:
        for piece in pack(text, CHUNK_CHARS):
            out.append({"chapter": ch.number, "file": rel, "page": page, "text": piece})
    return out


def build_chunks(
    chapters: Iterable["Chapter"],
    per_chapter: Sequence[Sequence[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Flatten chapter blocks into BM25-sized chunks carrying file/page provenance.

    ``per_chapter`` may carry chunks already built by a worker pool; ordering is
    always chapter order, so the parallel and serial results are identical.
    """
    chapters = list(chapters)
    if per_chapter is None:
        per_chapter = [chunks_for_chapter(ch) for ch in chapters]
    if len(per_chapter) != len(chapters):
        raise ValueError("per_chapter and chapters must be parallel sequences")
    chunks: list[dict[str, Any]] = []
    for part in per_chapter:
        chunks.extend(part)
    return chunks


def save_index(ws: str, chunks: Sequence[dict[str, Any]]) -> None:
    """Atomically write the chunk list to `<workspace>/index.json`."""
    from ..security.atomicio import atomic_write_json

    atomic_write_json(os.path.join(ws, INDEX_FILE),
                      {"version": 5, "chunks": list(chunks)})


def load_index(ws: str) -> list[dict[str, Any]]:
    """Load chunks from `<workspace>/index.json`; missing file means an empty index."""
    path = os.path.join(ws, INDEX_FILE)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("chunks", [])


def search(
    chunks: Sequence[dict[str, Any]],
    query: str,
    k: int = 5,
    chapter: int | None = None,
) -> list[tuple[dict[str, Any], float]]:
    """Return [(chunk, score)] ranked by BM25; empty list means the materials have no match."""
    pool = [c for c in chunks if chapter is None or c["chapter"] == chapter]
    if not pool:
        return []
    bm = BM25([tokenize(c["text"]) for c in pool])
    q = tokenize(query)
    return [(pool[i], s) for i, s in bm.best(q, k)]
