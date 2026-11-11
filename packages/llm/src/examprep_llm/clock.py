"""可注入的时钟。重试、限流、熔断都通过它取时间和睡眠，测试时换成虚拟时钟。"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """单调时间，单位秒。"""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


SYSTEM_CLOCK = SystemClock()
