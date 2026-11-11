"""预算与死循环保护（R6.7）。"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from examprep_llm import ToolCallBlock

Verdict = Literal["ok", "max_tool_calls", "loop_detected"]


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(default=20, ge=1)
    max_tool_calls: int = Field(default=50, ge=0)
    # 相同工具 + 相同参数连续出现达到该次数即停止（该次不执行）
    max_repeat_calls: int = Field(default=3, ge=2)


def call_signature(call: ToolCallBlock) -> str:
    args = (
        call.raw_arguments
        if call.raw_arguments is not None
        else json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    )
    return f"{call.name}:{args}"


class CallTracker:
    """单次 run 内的工具调用计数与重复检测。"""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.count = 0
        self._last: str | None = None
        self._repeat = 0

    def check(self, call: ToolCallBlock) -> Verdict:
        sig = call_signature(call)
        if sig == self._last:
            self._repeat += 1
        else:
            self._last = sig
            self._repeat = 1
        if self._repeat >= self.budget.max_repeat_calls:
            return "loop_detected"
        if self.count >= self.budget.max_tool_calls:
            return "max_tool_calls"
        self.count += 1
        return "ok"
