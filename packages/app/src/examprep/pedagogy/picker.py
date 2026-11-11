"""选题顺序（R3.5，移植 Skill _pick_quiz）：未解决的错题 → 没做过的题 → 做过的题；同级里真题优先。"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    qid: str
    chapter: int | None
    is_exam: bool


def pick_questions(
    pool: Sequence[Candidate],
    *,
    chapter: int | None,
    last_verdicts: Mapping[str, str],
    open_mistakes: Collection[str],
    exclude: Collection[str] = (),
    count: int = 1,
) -> list[str]:
    """``chapter`` 为 None 时从全课程选。"""

    def rank(c: Candidate) -> tuple[int, int, str]:
        exam_first = 0 if c.is_exam else 1
        if c.qid in open_mistakes:
            return (0, exam_first, c.qid)
        if c.qid not in last_verdicts:
            return (1, exam_first, c.qid)
        return (2, exam_first, c.qid)

    items = [c for c in pool if (chapter is None or c.chapter == chapter) and c.qid not in exclude]
    return [c.qid for c in sorted(items, key=rank)[:count]]
