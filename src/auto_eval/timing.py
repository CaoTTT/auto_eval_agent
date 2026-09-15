"""Per-case wall time, partitioned into observable stages without double counting.

The collector is inherited by asyncio children and preparation threads. Nested
admission waits take precedence over the surrounding model operation, so a long
local queue is never reported as model response time. Durations are wall time,
not CPU time or a sum of parallel workers' time.
"""
from __future__ import annotations

import threading
import time
import logging
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


_STAGES = ("request_wait", "media_queue", "media", "retry_wait", "model")
logger = logging.getLogger(__name__)


class StageTimings:
    def __init__(self, *, clock: Callable[[], float] = time.perf_counter,
                 on_change: Callable[[dict], None] | None = None,
                 started: bool = True):
        self._clock = clock
        self._on_change = on_change
        self._lock = threading.RLock()
        self._counts: Counter[str] = Counter()
        self._seconds = dict.fromkeys((*_STAGES, "other"), 0.0)
        self._started: float | None = clock() if started else None
        self._last = self._started
        self._finished = False
        self._attempts = 0

    def start(self) -> None:
        """Start after the case acquires its execution slot, excluding that queue."""
        with self._lock:
            if self._started is None:
                self._started = self._last = self._clock()
        self._notify()

    def _stage(self) -> str:
        return next((stage for stage in _STAGES if self._counts[stage]), "other")

    def _checkpoint(self) -> None:
        if self._started is None or self._finished:
            return
        now = self._clock()
        self._seconds[self._stage()] += max(0.0, now - self._last)
        self._last = now

    def snapshot(self) -> dict:
        with self._lock:
            self._checkpoint()
            return {
                **{f"{stage}_s": round(seconds, 3) for stage, seconds in self._seconds.items()},
                "total_s": round(sum(self._seconds.values()), 3),
                "attempts": self._attempts,
                "active_stage": self._stage() if self._started is not None and not self._finished else None,
                "measured_at": int(time.time() * 1000),
                "finished": self._finished,
            }

    def _notify(self) -> None:
        if self._on_change is not None and self._started is not None:
            try:
                self._on_change(self.snapshot())
            except Exception:
                # Diagnostics must not abort an otherwise healthy evaluation.
                logger.exception("failed to publish case timing update")

    def enter(self, stage: str) -> None:
        if stage not in _STAGES:
            raise ValueError(f"unknown timing stage: {stage}")
        with self._lock:
            self._checkpoint()
            self._counts[stage] += 1
        self._notify()

    def leave(self, stage: str) -> None:
        with self._lock:
            self._checkpoint()
            self._counts[stage] -= 1
        self._notify()

    def model_attempt(self) -> None:
        with self._lock:
            self._attempts += 1
        self._notify()

    def finish(self) -> dict:
        with self._lock:
            if self._finished:
                return self.snapshot()
            self._checkpoint()
            self._finished = True
        self._notify()
        return self.snapshot()


_current: ContextVar[StageTimings | None] = ContextVar("case_stage_timings", default=None)


@contextmanager
def collect_timings(collector: StageTimings) -> Iterator[StageTimings]:
    token = _current.set(collector)
    try:
        yield collector
    finally:
        collector.finish()
        _current.reset(token)


@contextmanager
def timing_span(stage: str) -> Iterator[None]:
    collector = _current.get()
    if collector is None:
        yield
        return
    collector.enter(stage)
    try:
        yield
    finally:
        collector.leave(stage)


def record_model_attempt() -> None:
    collector = _current.get()
    if collector is not None:
        collector.model_attempt()


def current_timings() -> dict | None:
    collector = _current.get()
    return collector.snapshot() if collector is not None else None
