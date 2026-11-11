"""取消令牌（R5.5、R6.6）。

``CancelToken`` 从 Agent 一路传到网关和工具。``cancellable()`` 把任意异步迭代器包装成可被令牌立即中断的迭代器：
源迭代器在独立任务里消费（provider 的 ``async with client.stream()`` 始终处于同一任务，避免 anyio 跨任务退出
cancel scope），令牌取消时直接取消该任务，HTTP 响应随生成器退出而关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any, TypeVar

from .errors import AbortedError

T = TypeVar("T")


class CancelToken:
    """协作式取消令牌，仅在单个事件循环内使用。"""

    def __init__(self, parent: CancelToken | None = None) -> None:
        self._cancelled = False
        self._reason: str | None = None
        self._callbacks: list[Callable[[], object]] = []
        self._event: asyncio.Event | None = None
        self._unlink: Callable[[], None] | None = None
        if parent is not None:
            if parent.cancelled:
                self.cancel(parent.reason or "cancelled")
            else:
                self._unlink = parent.on_cancel(lambda: self.cancel(parent.reason or "cancelled"))

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> None:
        if self._cancelled:
            return
        self._cancelled = True
        self._reason = reason
        if self._event is not None:
            self._event.set()
        callbacks, self._callbacks = self._callbacks, []
        for cb in callbacks:
            cb()

    def on_cancel(self, callback: Callable[[], object]) -> Callable[[], None]:
        """注册取消回调；已取消则立即调用。返回注销函数。"""
        if self._cancelled:
            callback()
            return lambda: None
        self._callbacks.append(callback)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

        return remove

    async def wait(self) -> None:
        if self._cancelled:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise AbortedError(self._reason or "cancelled")

    def child(self) -> CancelToken:
        return CancelToken(self)

    def detach(self) -> None:
        """与父令牌解除关联（子令牌用完后调用，避免父令牌上回调堆积）。"""
        if self._unlink is not None:
            self._unlink()
            self._unlink = None


_END: Any = object()


async def cancellable(source: AsyncIterator[T], token: CancelToken | None) -> AsyncIterator[T]:
    """令牌取消时立即中断 ``source`` 并抛出 :class:`AbortedError`。"""
    if token is None:
        async for item in source:
            yield item
        return
    token.raise_if_cancelled()

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in source:
                queue.put_nowait(("item", item))
            queue.put_nowait(("end", _END))
        except asyncio.CancelledError:
            queue.put_nowait(("aborted", None))
            raise
        except BaseException as exc:
            queue.put_nowait(("error", exc))
        finally:
            aclose = getattr(source, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(BaseException):
                    await aclose()

    task = asyncio.create_task(pump())
    remove = token.on_cancel(task.cancel)
    try:
        while True:
            kind, value = await queue.get()
            if token.cancelled:
                raise AbortedError(token.reason or "cancelled")
            if kind == "item":
                yield value
            elif kind == "end":
                return
            elif kind == "aborted":
                raise AbortedError(token.reason or "cancelled")
            else:
                raise value
    finally:
        remove()
        if not task.done():
            task.cancel()
        # gather + return_exceptions：吞掉任务自身的取消，但不吞掉外层对当前任务的取消
        await asyncio.gather(task, return_exceptions=True)
