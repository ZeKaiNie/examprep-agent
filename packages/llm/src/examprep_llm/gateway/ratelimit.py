"""每 provider 的令牌桶（RPM）与并发上限（R5.3）。"""

from __future__ import annotations

import asyncio
from types import TracebackType

from ..clock import SYSTEM_CLOCK, Clock


class TokenBucket:
    """令牌桶：每分钟补充 ``rpm`` 个令牌，最多存 ``capacity`` 个。"""

    def __init__(self, rpm: float, *, capacity: float | None = None, clock: Clock = SYSTEM_CLOCK) -> None:
        if rpm <= 0:
            raise ValueError("rpm 必须为正数")
        self.rate = rpm / 60.0
        self.capacity = capacity if capacity is not None else max(1.0, float(rpm))
        self._tokens = self.capacity
        self._clock = clock
        self._last = clock.now()

    def _refill(self) -> None:
        now = self._clock.now()
        if now > self._last:
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def try_acquire(self) -> bool:
        self._refill()
        if self._tokens >= 1.0 - 1e-9:  # 容忍浮点误差，避免多出一次极短的等待
            self._tokens = max(0.0, self._tokens - 1.0)
            return True
        return False

    async def acquire(self) -> None:
        while not self.try_acquire():
            await self._clock.sleep((1.0 - self._tokens) / self.rate)


class ProviderLimiter:
    """``async with limiter:`` 先占并发名额，再取令牌；名额一直持有到流结束。"""

    def __init__(
        self, *, rpm: int | None = None, max_concurrency: int | None = None, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self.bucket = TokenBucket(rpm, clock=clock) if rpm else None
        self.semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self.max_concurrency = max_concurrency
        self.in_flight = 0

    async def acquire(self) -> None:
        if self.semaphore is not None:
            await self.semaphore.acquire()
        try:
            if self.bucket is not None:
                await self.bucket.acquire()
        except BaseException:
            if self.semaphore is not None:
                self.semaphore.release()
            raise
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight -= 1
        if self.semaphore is not None:
            self.semaphore.release()

    async def __aenter__(self) -> ProviderLimiter:
        await self.acquire()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()
