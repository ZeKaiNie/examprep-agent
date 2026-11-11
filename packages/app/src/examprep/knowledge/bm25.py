"""纯 Python BM25（k1=1.5, b=0.75）。移植自 Skill v5.3 index.py。"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

K1, B = 1.5, 0.75


class BM25:
    def __init__(self, docs_tokens: Sequence[Sequence[str]]) -> None:
        self.n = len(docs_tokens)
        self.tf = [Counter(t) for t in docs_tokens]
        self.dl = [len(t) for t in docs_tokens]
        self.avgdl = (sum(self.dl) / self.n) if self.n else 1.0
        df: Counter[str] = Counter()
        for c in self.tf:
            df.update(c.keys())
        self.idf = {t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
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

    def best(self, query_tokens: Sequence[str], k: int = 5) -> list[tuple[int, float]]:
        """得分最高的 k 个文档 (下标, 得分)，只返回得分大于 0 的。"""
        sc = self.scores(query_tokens)
        order = sorted(range(self.n), key=lambda i: -sc[i])
        return [(i, sc[i]) for i in order[:k] if sc[i] > 0]
