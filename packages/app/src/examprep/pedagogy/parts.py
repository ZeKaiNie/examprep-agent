"""把一章切成可讲的“段”，每段由连续的证据块组成（R2）。

段与证据一一对应：Tutor 讲某一段时，上下文里的证据就是这一段的块，引用天然准确。
档位沿用 Skill v5.3 的 small / standard / large。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from ..knowledge.evidence import EvidenceChunk

Profile = Literal["small", "standard", "large"]

PROFILE_CHARS: dict[str, dict[str, int]] = {
    "small": {"zh": 1800, "en": 3000},
    "standard": {"zh": 3000, "en": 5000},
    "large": {"zh": 8000, "en": 12000},
}
QUIZ_N: dict[str, int] = {"small": 1, "standard": 3, "large": 5}


def pick_profile(context_window: int) -> Profile:
    """按 tutor 模型的上下文长度选档位：≤ 32k small，≤ 200k standard，更大 large。"""
    if context_window <= 32_000:
        return "small"
    if context_window <= 200_000:
        return "standard"
    return "large"


def part_chars(profile: str, language: str) -> int:
    table = PROFILE_CHARS.get(profile, PROFILE_CHARS["standard"])
    return table["zh" if language == "zh" else "en"]


def split_parts(chunks: Sequence[EvidenceChunk], target: int) -> list[list[EvidenceChunk]]:
    """按顺序把证据块装入段，累计字数超过 ``target`` 就开新段；单块超长也自成一段。"""
    parts: list[list[EvidenceChunk]] = []
    cur: list[EvidenceChunk] = []
    size = 0
    for c in chunks:
        if cur and size + len(c.text) > target:
            parts.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c.text)
    if cur:
        parts.append(cur)
    return parts
