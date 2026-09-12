"""Shared, paced admission for Bailian requests (not a task scheduler).

Token counts before sending are estimates; returned usage settles reservations.
Every HTTP attempt, including repair and compatibility retries, uses this gate.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from PIL import Image

from .observability import log_event


MODEL = "qwen3.5-397b-a17b"
DEFAULT_CONCURRENCY = 128
PREPARATION_CONCURRENCY = 4


def supports_bailian_pacing(cfg) -> bool:
    host = (urlparse(getattr(cfg, "base_url", "") or "").hostname or "").lower()
    return (getattr(cfg, "model", "") or "").lower() == MODEL and (
        host == "dashscope.aliyuncs.com"
        or host.endswith(".aliyuncs.com") and (
            host.startswith("dashscope-") or ".maas." in host
        )
    )


def recommended_concurrency(judges) -> int:
    return DEFAULT_CONCURRENCY if judges and all(supports_bailian_pacing(j) for j in judges) else 4


class _ActiveClock:
    def __init__(self):
        self.started = time.monotonic()
        self.paused_at = None
        self.paused = 0.0
        self.depth = 0
        self.changed = asyncio.Event()

    def elapsed(self):
        return (self.paused_at if self.depth else time.monotonic()) - self.started - self.paused


_active_clock: ContextVar[_ActiveClock | None] = ContextVar("llm_active_clock", default=None)


@contextmanager
def admission_wait():
    """Exclude local admission/cooldown waits from the outer evaluation deadline."""
    clock = _active_clock.get()
    if clock is not None:
        if clock.depth == 0:
            clock.paused_at = time.monotonic()
        clock.depth += 1
        clock.changed.set()
    try:
        yield
    finally:
        if clock is not None:
            clock.depth -= 1
            if clock.depth == 0:
                clock.paused += time.monotonic() - clock.paused_at
                clock.paused_at = None
            clock.changed.set()


async def wait_for_active(awaitable, timeout: float):
    """Like wait_for, with a deadline paused only by explicit admission waits."""
    clock = _ActiveClock()
    token = _active_clock.set(clock)
    try:
        task = asyncio.ensure_future(awaitable)
    finally:
        _active_clock.reset(token)
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


def estimate_input_tokens(kwargs: dict) -> int:
    """Local conservative proxy; never count base64 characters as text tokens.

    Image geometry follows the existing 32-pixel patch guardrail. Unknown remote
    images reserve 16K tokens; no URL is fetched here and evidence is not changed.
    """
    count = 256
    for message in kwargs.get("messages", []):
        content = message.get("content") or ""
        if isinstance(content, str):
            count += len(content.encode("utf-8"))
            continue
        for part in content:
            if part.get("type") == "text":
                count += len(part.get("text", "").encode("utf-8"))
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                try:
                    if not url.startswith("data:image/"):
                        raise ValueError("remote image")
                    # Only inspect the header; do not decode pixels or rescale.
                    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as picture:
                        width, height = picture.size
                    count += math.ceil(width / 32) * math.ceil(height / 32) + 256
                except (ValueError, OSError):
                    count += 16_384
    return count


@dataclass
class Reservation:
    sent: float
    tokens: int
    input_proxy: int = 0
    kind: str = "text"


class RequestThrottle:
    def __init__(self, *, rpm: int = 480, tpm: int = 800_000,
                 max_inflight: int = DEFAULT_CONCURRENCY, warmup_s: float = 15.0,
                 clock=time.monotonic, sleep=asyncio.sleep):
        self.rpm, self.tpm = rpm, tpm
        self._clock, self._sleep = clock, sleep
        self._slots = asyncio.Semaphore(max_inflight)
        self._lock = asyncio.Lock()
        self._records: deque[Reservation] = deque()
        self._next_send = 0.0
        self._last_send = None
        self._warm_started = clock()
        self._warmup_s = warmup_s
        self._cooldown_until = 0.0
        self._scale = 1.0
        self._last_recovery = clock()
        self._output_estimate = 8192.0
        self._input_scale = {"text": 1.0, "vision": 1.0}

    def estimate(self, input_tokens: int, kind: str = "text") -> int:
        return math.ceil(input_tokens * self._input_scale[kind] + self._output_estimate)

    async def acquire(self, tokens: int, *, input_proxy: int = 0, kind: str = "text") -> Reservation:
        if tokens > self.tpm:
            raise ValueError(f"单次预估 {tokens} Token 超过本地每分钟预算 {self.tpm}，请检查输入大小")
        with admission_wait():
            await self._slots.acquire()
            try:
                # Sleep under the admission lock: cancelled waiters cannot leave
                # reserved future slots, and wake-ups cannot send a burst.
                async with self._lock:
                    while True:
                        now = self._clock()
                        while self._records and self._records[0].sent <= now - 60:
                            self._records.popleft()
                        delay = max(self._next_send, self._cooldown_until) - now
                        total = sum(record.tokens for record in self._records)
                        if len(self._records) >= self.rpm or total + tokens > self.tpm:
                            delay = max(delay, self._records[0].sent + 60 - now)
                        if delay <= 0:
                            break
                        await self._sleep(delay)
                    if self._last_send is None or now - self._last_send > 30:
                        self._warm_started = now
                    warm = min(1.0, .25 + .75 * (now - self._warm_started) / self._warmup_s) if self._warmup_s else 1.0
                    interval = max(60 / self.rpm, tokens * 60 / self.tpm) / (warm * self._scale)
                    self._next_send = now + interval
                    self._last_send = now
                    record = Reservation(now, tokens, input_proxy, kind)
                    self._records.append(record)
                    return record
            except BaseException:
                self._slots.release()
                raise

    def finish(self, record: Reservation, usage=None, error: BaseException | None = None):
        try:
            now = self._clock()
            def value(key):
                raw = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
                return raw if isinstance(raw, (int, float)) and raw >= 0 else None
            prompt, completion = value("prompt_tokens"), value("completion_tokens")
            total = value("total_tokens")
            if total is None and prompt is not None and completion is not None:
                total = prompt + completion
            if total is not None:
                actual = math.ceil(total)
                if record.sent > now - 60:
                    record.tokens = actual
                else:
                    # A long stream may emit output in a later minute. Charge
                    # its output now rather than silently forgetting it.
                    self._records.append(Reservation(now, math.ceil(completion if completion is not None else actual)))
                if actual > self.tpm:
                    self._cooldown_until = max(self._cooldown_until, now + 60)
            if completion is not None:
                # Keep a margin and react immediately to larger-than-usual output.
                self._output_estimate = max(1024, completion * 1.2, self._output_estimate * .9 + completion * 1.2 * .1)
            if prompt is not None and record.input_proxy:
                observed = prompt / record.input_proxy * 1.2
                previous = self._input_scale[record.kind]
                # Calibrate text and visual requests separately; increase quickly,
                # reduce slowly, retain a margin. This is not a billing tokenizer.
                self._input_scale[record.kind] = max(.25, observed, previous * .9 + observed * .1)
            status = getattr(error, "status_code", None)
            detail = str(error).lower() if error else ""
            congested = status in {429, 503} or any(s in detail for s in ("throttling", "rate limit", "rate_limit", "limit_requests", "limit_burst_rate"))
            if congested:
                response = getattr(error, "response", None)
                retry_after = (getattr(response, "headers", {}) or {}).get("retry-after")
                wait = 5.0
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        try:
                            wait = max(wait, parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                if now >= self._cooldown_until:
                    self._scale = max(.1, self._scale * .5)
                self._cooldown_until = max(self._cooldown_until, now + wait)
                self._last_recovery = self._cooldown_until
                log_event("请求调度", "限流降速", details={"冷却秒数": wait, "速率比例": self._scale})
            elif error is None and now >= self._cooldown_until and now - self._last_recovery >= 10:
                self._scale = min(1.0, self._scale + .1)
                self._last_recovery = now
        finally:
            self._slots.release()


def shared_throttle(cfg) -> RequestThrottle | None:
    if not supports_bailian_pacing(cfg):
        return None
    # Share across keys, clients, repairs and consecutive tasks on this loop.
    # Different accounts/regions also share conservatively, since credentials do
    # not reliably identify an Alibaba primary account. No secrets are retained.
    loop = asyncio.get_running_loop()
    # Store on the loop, not a global weak-key dict whose value's asyncio locks
    # could strongly reference that same loop and retain closed test/service loops.
    controllers = getattr(loop, "_auto_eval_request_throttles", None)
    if controllers is None:
        controllers = {}
        setattr(loop, "_auto_eval_request_throttles", controllers)
    if MODEL not in controllers:
        controllers[MODEL] = RequestThrottle()
    return controllers[MODEL]
