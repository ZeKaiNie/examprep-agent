"""统一错误类型（R3.4）。

``retryable`` 表示同一请求原样重发有可能成功；网关只对这类错误做重试。
"""

from __future__ import annotations

from typing import ClassVar


class LLMError(Exception):
    retryable: ClassVar[bool] = False
    kind: ClassVar[str] = "error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.provider = provider
        self.model = model
        self.body = body

    def __str__(self) -> str:
        where = "/".join(x for x in (self.provider, self.model) if x)
        prefix = f"[{self.kind}"
        if self.status is not None:
            prefix += f" {self.status}"
        if where:
            prefix += f" {where}"
        return f"{prefix}] {self.message}"


class AuthenticationError(LLMError):
    kind = "auth"


class RateLimitError(LLMError):
    retryable = True
    kind = "rate_limit"

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status: int | None = None,
        provider: str | None = None,
        model: str | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message, status=status, provider=provider, model=model, body=body)
        self.retry_after = retry_after


class ContextOverflowError(LLMError):
    kind = "context_overflow"


class BadRequestError(LLMError):
    kind = "bad_request"


class ServerError(LLMError):
    retryable = True
    kind = "server"


class LLMTimeoutError(LLMError):
    retryable = True
    kind = "timeout"


class NetworkError(LLMError):
    retryable = True
    kind = "network"


class AbortedError(LLMError):
    kind = "aborted"


class NoModelAvailableError(LLMError):
    kind = "no_model"
