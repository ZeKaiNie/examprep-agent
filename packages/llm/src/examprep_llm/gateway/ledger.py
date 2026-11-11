"""调用记账：每次模型调用的用量、成本、耗时（R5.6）。"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Literal

from ..registry import Cost
from ..types import Usage

Outcome = Literal["ok", "error", "aborted"]


@dataclass(frozen=True)
class CallRecord:
    role: str | None
    model_ref: str
    outcome: Outcome
    attempts: int
    usage: Usage = field(default_factory=Usage)
    cost: Cost | None = None
    latency: float = 0.0  # 秒，从发起到结束
    ttft: float | None = None  # 首个内容事件的延迟
    error_type: str | None = None
    fallback_from: tuple[str, ...] = ()


class UsageLedger:
    def __init__(self, *, keep: int = 1000) -> None:
        self.records: deque[CallRecord] = deque(maxlen=keep)
        self._by_role: dict[str, Usage] = defaultdict(Usage)
        self._by_model: dict[str, Usage] = defaultdict(Usage)
        self._cost: dict[str, float] = defaultdict(float)
        self._total = Usage()
        self.calls = 0
        self.errors = 0

    def add(self, record: CallRecord) -> None:
        self.records.append(record)
        self.calls += 1
        if record.outcome == "error":
            self.errors += 1
        self._total = self._total + record.usage
        self._by_role[record.role or "-"] = self._by_role[record.role or "-"] + record.usage
        self._by_model[record.model_ref] = self._by_model[record.model_ref] + record.usage
        if record.cost is not None:
            self._cost[record.cost.currency] += record.cost.total

    def total(self) -> Usage:
        return self._total

    def by_role(self) -> dict[str, Usage]:
        return dict(self._by_role)

    def by_model(self) -> dict[str, Usage]:
        return dict(self._by_model)

    def cost(self) -> dict[str, float]:
        """按币种汇总的成本。"""
        return dict(self._cost)
