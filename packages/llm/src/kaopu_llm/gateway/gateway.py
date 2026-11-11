"""模型网关：按角色路由、降级链、重试、限流、熔断、取消、记账（R5）。"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..cancel import CancelToken, cancellable
from ..clock import SYSTEM_CLOCK, Clock
from ..config import provider_config
from ..errors import AbortedError, AuthenticationError, LLMError, NoModelAvailableError, ServerError
from ..events import DoneEvent, StartEvent, StreamEvent
from ..protocols import Provider, build_provider
from ..registry import ModelInfo, ModelRegistry, compute_cost, parse_model_ref
from ..types import AssistantMessage, LLMRequest, Usage
from .breaker import BreakerPolicy, CircuitBreaker
from .ledger import CallRecord, Outcome, UsageLedger
from .ratelimit import ProviderLimiter
from .retry import RetryPolicy, backoff_delay

T = TypeVar("T")

ROLES = ("tutor", "grader", "verifier", "extractor", "summarizer")


class GatewayConfig(BaseModel):
    """可从 TOML/JSON 加载的网关配置。``providers`` 的每一项会与同名预设合并。"""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, dict[str, Any]] = Field(default_factory=dict[str, dict[str, Any]])
    models: list[ModelInfo] = Field(default_factory=list[ModelInfo])
    roles: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    breaker: BreakerPolicy = Field(default_factory=BreakerPolicy)


async def _race(
    aw: Awaitable[T], cancel: CancelToken | None, *, on_abandon: Callable[[], None] | None = None
) -> T:
    """等待 ``aw``，令牌取消时立即抛出 AbortedError。若放弃时 ``aw`` 恰好已完成，调用 ``on_abandon`` 清理。"""
    if cancel is None:
        return await aw
    cancel.raise_if_cancelled()
    task: asyncio.Future[T] = asyncio.ensure_future(aw)
    remove = cancel.on_cancel(task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        if cancel.cancelled:
            raise AbortedError(cancel.reason or "cancelled") from None
        raise
    finally:
        remove()
        abandoned = cancel.cancelled and task.done() and not task.cancelled() and task.exception() is None
        if abandoned and on_abandon is not None:
            on_abandon()


class Gateway:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        roles: Mapping[str, Sequence[str]] | None = None,
        *,
        registry: ModelRegistry | None = None,
        retry: RetryPolicy | None = None,
        breaker: BreakerPolicy | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.roles: dict[str, list[str]] = {k: list(v) for k, v in (roles or {}).items()}
        self.registry = registry or ModelRegistry()
        self.retry = retry or RetryPolicy()
        self.breaker_policy = breaker or BreakerPolicy()
        self.clock = clock or SYSTEM_CLOCK
        self.rng = rng or random.Random()
        self.ledger = UsageLedger()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._limiters: dict[str, ProviderLimiter] = {}
        self._callbacks: list[Callable[[CallRecord], None]] = []

    @classmethod
    def from_config(
        cls,
        config: GatewayConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> Gateway:
        providers: dict[str, Provider] = {}
        for name, fields in config.providers.items():
            providers[name] = build_provider(provider_config(name, **fields), transport=transport)
        return cls(
            providers,
            config.roles,
            registry=ModelRegistry(config.models),
            retry=config.retry,
            breaker=config.breaker,
            clock=clock,
            rng=rng,
        )

    # ------------------------------------------------------------ 公共接口

    def on_call(self, callback: Callable[[CallRecord], None]) -> None:
        self._callbacks.append(callback)

    def breaker(self, model_ref: str) -> CircuitBreaker:
        b = self._breakers.get(model_ref)
        if b is None:
            b = self._breakers[model_ref] = CircuitBreaker(self.breaker_policy, clock=self.clock)
        return b

    def limiter(self, provider: str) -> ProviderLimiter:
        lim = self._limiters.get(provider)
        if lim is None:
            cfg = self.providers[provider].config
            lim = self._limiters[provider] = ProviderLimiter(
                rpm=cfg.rpm, max_concurrency=cfg.max_concurrency, clock=self.clock
            )
        return lim

    def model_info(self, model_ref: str) -> ModelInfo:
        provider_name, model_id = parse_model_ref(model_ref)
        provider = self._provider(provider_name)
        return self.registry.get(provider_name, model_id, provider.config.protocol)

    def chain(self, *, role: str | None = None, model: str | None = None) -> list[str]:
        if model is not None:
            return [model]
        if role is None:
            raise ValueError("必须指定 role 或 model")
        chain = self.roles.get(role)
        if not chain:
            raise ValueError(f"角色 {role!r} 没有配置模型")
        return list(chain)

    async def complete(
        self,
        request: LLMRequest,
        *,
        role: str | None = None,
        model: str | None = None,
        cancel: CancelToken | None = None,
    ) -> AssistantMessage:
        message: AssistantMessage | None = None
        async for ev in self.stream(request, role=role, model=model, cancel=cancel):
            if isinstance(ev, DoneEvent):
                message = ev.message
        assert message is not None
        return message

    async def stream(
        self,
        request: LLMRequest,
        *,
        role: str | None = None,
        model: str | None = None,
        cancel: CancelToken | None = None,
    ) -> AsyncIterator[StreamEvent]:
        chain = self.chain(role=role, model=model)
        last_error: LLMError | None = None
        skipped: list[str] = []
        tried: list[str] = []
        for ref in chain:
            provider_name, model_id = parse_model_ref(ref)
            provider = self._provider(provider_name)
            info = self.registry.get(provider_name, model_id, provider.config.protocol)
            breaker = self.breaker(ref)
            failures = 0
            while True:
                if not breaker.allow():
                    if failures == 0:
                        skipped.append(ref)
                    break
                if cancel is not None:
                    cancel.raise_if_cancelled()
                started = self.clock.now()
                emitted = False
                ttft: float | None = None
                done: AssistantMessage | None = None
                limiter = self.limiter(provider_name)
                try:
                    await _race(limiter.acquire(), cancel, on_abandon=limiter.release)
                    try:
                        pending_start: StartEvent | None = None
                        async for ev in cancellable(provider.stream(request, info), cancel):
                            if isinstance(ev, StartEvent):
                                # 暂扣 StartEvent：直到出现内容前出错，仍可安全重试
                                pending_start = ev
                                continue
                            if not emitted:
                                emitted = True
                                ttft = self.clock.now() - started
                                if pending_start is not None:
                                    yield pending_start
                            if isinstance(ev, DoneEvent):
                                done = ev.message
                            yield ev
                    finally:
                        limiter.release()
                    if done is None:
                        raise ServerError("provider 未给出完成事件", provider=provider_name, model=model_id)
                except AbortedError:
                    breaker.release()
                    self._record(role, ref, "aborted", failures + 1, started, ttft, None, "aborted", tried)
                    raise
                except LLMError as exc:
                    counts = exc.retryable or isinstance(exc, AuthenticationError)
                    if counts:
                        breaker.record_failure()
                    else:
                        breaker.release()
                    failures += 1
                    self._record(role, ref, "error", failures, started, ttft, None, exc.kind, tried)
                    if emitted or not counts:
                        raise
                    last_error = exc
                    if not exc.retryable or failures >= self.retry.max_attempts:
                        break  # 鉴权/额度错误或重试用尽：降级
                    delay = backoff_delay(
                        self.retry, failures, self.rng, retry_after=getattr(exc, "retry_after", None)
                    )
                    if delay is None:
                        break
                    await _race(self.clock.sleep(delay), cancel)
                    continue
                except BaseException:
                    breaker.release()
                    raise
                breaker.record_success()
                assert done is not None
                self._record(role, ref, "ok", failures + 1, started, ttft, done, None, tried, info=info)
                return
            tried.append(ref)
        if last_error is not None:
            raise last_error
        raise NoModelAvailableError(f"降级链上的模型都处于熔断状态：{', '.join(skipped)}")

    async def aclose(self) -> None:
        for p in self.providers.values():
            with contextlib.suppress(Exception):
                await p.aclose()

    # ------------------------------------------------------------ 内部

    def _provider(self, name: str) -> Provider:
        try:
            return self.providers[name]
        except KeyError:
            raise ValueError(f"未配置 provider {name!r}") from None

    def _record(
        self,
        role: str | None,
        ref: str,
        outcome: Outcome,
        attempts: int,
        started: float,
        ttft: float | None,
        message: AssistantMessage | None,
        error_type: str | None,
        tried: list[str],
        *,
        info: ModelInfo | None = None,
    ) -> None:
        usage = message.usage if message is not None else Usage()
        cost = compute_cost(usage, info.pricing) if info is not None else None
        record = CallRecord(
            role=role,
            model_ref=ref,
            outcome=outcome,
            attempts=attempts,
            usage=usage,
            cost=cost,
            latency=self.clock.now() - started,
            ttft=ttft,
            error_type=error_type,
            fallback_from=tuple(tried),
        )
        self.ledger.add(record)
        for cb in self._callbacks:
            cb(record)
