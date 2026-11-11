"""熔断器：每个模型独立（R5.4）。

closed --连续失败达阈值--> open --冷却结束--> half_open（只放行一个试探请求）
half_open --试探成功--> closed；--试探失败--> open
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..clock import SYSTEM_CLOCK, Clock

BreakerState = Literal["closed", "open", "half_open"]


class BreakerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    failure_threshold: int = Field(default=5, ge=1)
    cooldown: float = Field(default=30.0, ge=0)


class CircuitBreaker:
    def __init__(self, policy: BreakerPolicy | None = None, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.policy = policy or BreakerPolicy()
        self._clock = clock
        self._state: BreakerState = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._state == "open" and self._clock.now() - self._opened_at >= self.policy.cooldown:
            self._state = "half_open"
            self._probe_in_flight = False
        return self._state

    @property
    def failures(self) -> int:
        return self._failures

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "open":
            return False
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._state = "closed"
        self._failures = 0
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self.state == "half_open":
            self._open()
            return
        self._failures += 1
        if self._failures >= self.policy.failure_threshold:
            self._open()

    def release(self) -> None:
        """请求结束但不计成败（取消、调用方自身的请求错误）。"""
        self._probe_in_flight = False

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = self._clock.now()
