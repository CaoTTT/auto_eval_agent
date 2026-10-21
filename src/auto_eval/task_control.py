"""Cooperative task pause at preparation and unsent-request boundaries.

A pause is local to its async call chain. Response streams already dispatched
must never be wrapped in ``pause_aware``: they finish and settle normally.

Also hosts the deadline helpers ``wait_for_active`` / ``admission_wait`` that
let an outer evaluation timeout exclude local admission/cooldown waits. These
were originally part of the Bailian-specific request throttle and are kept
here (independent of any limiter) because the web runner and stream layer use
them for per-item / per-request timeouts.
"""
from __future__ import annotations

import asyncio
import time
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


class _ActiveClock:
    def __init__(self):
        self.started = time.monotonic()
        self.paused_at = None
        self.paused = 0.0
        self.depth = 0
        self.changed = asyncio.Event()

    def elapsed(self):
        return (self.paused_at if self.depth else time.monotonic()) - self.started - self.paused


_active_clocks: ContextVar[tuple[_ActiveClock, ...]] = ContextVar("llm_active_clocks", default=())


@contextmanager
def admission_wait():
    """Exclude local admission/cooldown waits from the outer evaluation deadline."""
    clocks = _active_clocks.get()
    for clock in clocks:
        if clock.depth == 0:
            clock.paused_at = time.monotonic()
        clock.depth += 1
        clock.changed.set()
    try:
        yield
    finally:
        for clock in clocks:
            clock.depth -= 1
            if clock.depth == 0:
                clock.paused += time.monotonic() - clock.paused_at
                clock.paused_at = None
            clock.changed.set()


async def wait_for_active(awaitable: Awaitable[_T], timeout: float) -> _T:
    """Like wait_for, with a deadline paused only by explicit admission waits."""
    clock = _ActiveClock()
    token = _active_clocks.set((*_active_clocks.get(), clock))
    try:
        task = asyncio.ensure_future(awaitable)
    finally:
        _active_clocks.reset(token)
    signal = None
    try:
        while not task.done():
            clock.changed.clear()
            remaining = timeout - clock.elapsed()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            signal = asyncio.create_task(clock.changed.wait())
            await asyncio.wait({task, signal}, timeout=None if clock.depth else remaining,
                               return_when=asyncio.FIRST_COMPLETED)
            signal.cancel()
            await asyncio.gather(signal, return_exceptions=True)
        return task.result()
    finally:
        if signal is not None:
            signal.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *([signal] if signal else []), return_exceptions=True)
