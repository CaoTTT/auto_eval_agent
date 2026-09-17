"""Cooperative task pause at preparation and unsent-request boundaries.

A pause is local to its async call chain. Response streams already dispatched
must never be wrapped in ``pause_aware``: they finish and settle normally.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar


class PauseRequested(asyncio.CancelledError):
    """Unsent work stopped by a user pause, rather than a failed evaluation."""


_pause_check: ContextVar[Callable[[], bool] | None] = ContextVar(
    "auto_eval_pause_check", default=None,
)
_T = TypeVar("_T")


@contextmanager
def bind_pause_check(check: Callable[[], bool]) -> Iterator[None]:
    token = _pause_check.set(check)
    try:
        yield
    finally:
        _pause_check.reset(token)


def check_pause() -> None:
    check = _pause_check.get()
    if check is not None and check():
        raise PauseRequested("任务已请求暂停，尚未发送的模型请求已停止")


async def pause_aware(
    awaitable: Awaitable[_T], *, on_cancel: Callable[[_T], None] | None = None,
) -> _T:
    """Interrupt an unsent wait, retaining ownership of completed resources.

Completion wins a simultaneous pause: callers record ownership first, then
call ``check_pause`` inside their cleanup scope. For resource acquisitions,
``on_cancel`` releases a result obtained simultaneously with external cancel.
"""
    if _pause_check.get() is None:
        return await awaitable
    operation = asyncio.ensure_future(awaitable)
    try:
        while not operation.done():
            check_pause()
            await asyncio.wait({operation}, timeout=0.05)
        return operation.result()
    except BaseException:
        if not operation.done():
            operation.cancel()
        # Await cleanup, including locks that return a permit during cancellation.
        await asyncio.gather(operation, return_exceptions=True)
        if on_cancel is not None and not operation.cancelled() and operation.exception() is None:
            on_cancel(operation.result())
        raise
