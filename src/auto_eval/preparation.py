"""Cooperative preparation cancellation and owned media subprocess lifetimes."""
from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar


T = TypeVar("T")


class PreparationStopped(TimeoutError):
    """Stop preparation without falling back to another extraction strategy."""


class _Scope:
    def __init__(self, timeout: float):
        self.deadline = time.monotonic() + timeout
        self.cancelled = threading.Event()

    def stopped(self) -> bool:
        return self.cancelled.is_set() or time.monotonic() >= self.deadline


_scope: ContextVar[_Scope | None] = ContextVar("preparation_scope", default=None)
_limit: ContextVar[asyncio.Semaphore | None] = ContextVar("preparation_limit", default=None)


@contextmanager
def preparation_limit(limit: asyncio.Semaphore | None):
    token = _limit.set(limit)
    try:
        yield
    finally:
        _limit.reset(token)


def check_preparation() -> None:
    scope = _scope.get()
    if scope is not None and scope.stopped():
        raise PreparationStopped("视觉证据准备已取消或超时")


async def run_preparation(fn: Callable[..., T], *args, timeout: float, **kwargs) -> T:
    limit = _limit.get()
    if limit is None:
        return await _run_preparation(fn, *args, timeout=timeout, **kwargs)
    from .request_throttle import admission_wait

    with admission_wait():
        await limit.acquire()
    try:
        return await _run_preparation(fn, *args, timeout=timeout, **kwargs)
    finally:
        limit.release()


async def _run_preparation(fn: Callable[..., T], *args, timeout: float, **kwargs) -> T:
    """Keep ownership of the worker until it has released its resources.

    Cancellation cannot interrupt a Pillow/NumPy operation mid-call. Checkpoints
    stop subsequent work, and the caller waits for that operation to unwind.
    Waiting without cancelling the worker also covers queued executor jobs.
    """
    scope = _Scope(timeout)

    def work():
        token = _scope.set(scope)
        try:
            check_preparation()
            result = fn(*args, **kwargs)
            check_preparation()
            return result
        finally:
            _scope.reset(token)

    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        done, _ = await asyncio.wait({worker}, timeout=timeout)
        if not done:
            raise TimeoutError("视觉证据准备超时")
        return worker.result()
    except (asyncio.TimeoutError, asyncio.CancelledError):
        scope.cancelled.set()
        # Even a repeated shutdown cancellation must not detach the worker.
        while not worker.done():
            try:
                await asyncio.wait({worker})
            except asyncio.CancelledError:
                continue
        if not worker.cancelled():
            worker.exception()  # Retrieve its error; preserve the caller's cause.
        raise


@contextmanager
def media_process(command, *, timeout: float | None = None, **kwargs) -> Iterator[subprocess.Popen]:
    """Kill and reap this exact child on deadline/cancellation, even on pipe stalls.

    The watchdog owns no task context; it is joined before returning. Streaming
    stderr stays streaming, so long videos do not require buffering all logs.
    """
    check_preparation()
    scope = _scope.get()
    deadline = time.monotonic() + (300 if timeout is None else timeout)
    if scope is not None:
        deadline = scope.deadline if timeout is None else min(deadline, scope.deadline)
    done, stopped = threading.Event(), threading.Event()
    with subprocess.Popen(command, **kwargs) as proc:
        def watch():
            while not done.wait(.05):
                if time.monotonic() >= deadline or (scope is not None and scope.stopped()):
                    stopped.set()
                    try:
                        proc.kill()
                    except OSError:
                        pass  # It may have exited between the check and kill.
                    return

        watcher = threading.Thread(target=watch, name="media-process-watch", daemon=True)
        watcher.start()
        try:
            yield proc
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            finally:
                done.set()
                watcher.join()
            check_preparation()
            if stopped.is_set():
                raise PreparationStopped("视频处理子进程超时，已终止并回收")


def run_media_command(command, *, timeout: float | None = None, check: bool = False,
                      capture_output: bool = False, **kwargs) -> subprocess.CompletedProcess:
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with media_process(command, timeout=timeout, **kwargs) as proc:
        stdout, stderr = proc.communicate()
        if check and proc.returncode:
            raise subprocess.CalledProcessError(proc.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
