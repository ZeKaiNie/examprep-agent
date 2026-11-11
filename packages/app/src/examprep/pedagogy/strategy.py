"""策略矩阵：起点模式 × 剩余时间 → 状态机参数（05 文档第 2 节，复活 Skill v4.3 的 3 × 4）。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

StartLevel = Literal["zero", "weak", "gap"]
Mode = Literal["cram", "longterm"]
Bucket = Literal["1d", "3d", "7d", "long"]


@dataclass(frozen=True, slots=True)
class Strategy:
    check_every: int  # 每讲几段出一次检查题；0 表示不检查
    questions_per_chapter: int  # 一章讲完练几题；0 表示只看例题不练
    hint_ladder: bool  # 先给提示再揭晓答案
    practice_first: bool  # 查缺：先测后讲
    bucket: Bucket


def days_left(exam_date: str | None, today: dt.date | None = None) -> int | None:
    if not exam_date:
        return None
    try:
        target = dt.date.fromisoformat(exam_date)
    except ValueError:
        return None
    return max(0, (target - (today or dt.date.today())).days)


def bucket_of(mode: str, days: int | None) -> Bucket:
    if mode == "longterm" or days is None:
        return "long"
    if days <= 1:
        return "1d"
    if days <= 3:
        return "3d"
    if days <= 7:
        return "7d"
    return "long"


# (check_every, questions_per_chapter)
_MATRIX: dict[str, dict[str, tuple[int, int]]] = {
    "zero": {"1d": (0, 1), "3d": (2, 3), "7d": (1, 5), "long": (1, 5)},
    "weak": {"1d": (0, 1), "3d": (0, 2), "7d": (2, 3), "long": (1, 3)},
    "gap": {"1d": (0, 3), "3d": (0, 3), "7d": (0, 3), "long": (2, 3)},
}


def strategy_for(
    start_level: str, mode: str, exam_date: str | None, today: dt.date | None = None
) -> Strategy:
    bucket = bucket_of(mode, days_left(exam_date, today))
    row = _MATRIX.get(start_level, _MATRIX["zero"])
    check_every, n_questions = row[bucket]
    return Strategy(
        check_every=check_every,
        questions_per_chapter=n_questions,
        hint_ladder=bucket == "long",  # 长期模式默认开启，突击模式时间不够默认关闭
        practice_first=start_level == "gap",
        bucket=bucket,
    )
