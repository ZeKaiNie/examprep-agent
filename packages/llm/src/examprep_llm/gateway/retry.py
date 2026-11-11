"""重试策略：指数退避 + 抖动，尊重 retry-after（R5.2）。"""

from __future__ import annotations

import random

from pydantic import BaseModel, ConfigDict, Field


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=3, ge=1)  # 每个模型的最多尝试次数（含首次）
    base_delay: float = Field(default=0.5, ge=0)
    max_delay: float = Field(default=20.0, ge=0)
    # retry-after 超过该值时不等待，直接降级到下一个模型
    max_retry_after: float = Field(default=60.0, ge=0)


def backoff_delay(
    policy: RetryPolicy, failures: int, rng: random.Random, *, retry_after: float | None = None
) -> float | None:
    """第 ``failures`` 次失败后的等待秒数；返回 None 表示不值得等待。

    抖动取 [0.5, 1.0] 倍，既分散并发请求，又保证不低于一半的退避时间。
    """
    exp = min(policy.max_delay, policy.base_delay * (2 ** max(0, failures - 1)))
    delay = exp * rng.uniform(0.5, 1.0)
    if retry_after is not None:
        if retry_after > policy.max_retry_after:
            return None
        return max(retry_after, delay)
    return delay
