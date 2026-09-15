"""Shared, paced admission for Bailian requests (not a task scheduler).

Token counts before sending are estimates; returned usage settles reservations.
Every HTTP attempt, including repair and compatibility retries, uses this gate.
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .observability import log_event
from .token_estimation import estimate_input_tokens
from .timing import timing_span


MODEL = "qwen3.5-397b-a17b"
DEFAULT_CONCURRENCY = 128
PREPARATION_CONCURRENCY = 4
SECOND_REQUEST_LIMIT = 9
DEFAULT_RPM = 480
DEFAULT_TPM = 800_000
MAX_RPM = 540
MAX_TPM = 900_000
DISPATCH_GUARD_S = .001


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


async def wait_for_active(awaitable, timeout: float):
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


@dataclass
class Reservation:
    sent: float
    tokens: int
    input_proxy: int = 0
    kind: str = "text"
    finished: bool = False
    owner: object | None = None
    interval: float = 0.0
    headers_fenced: bool = False
    initial_tokens: int = 0
    input_tokens: int = 0
    input_confirmed_at: float | None = None
    input_charge: _TokenCharge | None = None


@dataclass
class _TokenCharge:
    sent: float
    tokens: int


class RequestThrottle:
    """A process/event-loop budget, called at the actual HTTP dispatch boundary.

    Request timestamps and token charges have independent lifetimes. Confirmed
    input keeps a 60-second window; output stays reserved until completion. Input
    without a server acknowledgement stays reserved too. This is not a distributed
    account limiter: all managed sends must use this controller and event loop.
    """

    def __init__(self, *, rpm: int = DEFAULT_RPM, tpm: int = DEFAULT_TPM,
                 max_inflight: int = DEFAULT_CONCURRENCY, warmup_s: float = 15.0,
                 clock=time.monotonic, sleep=asyncio.sleep, adaptive: bool = False,
                 max_rpm: int | None = None, max_tpm: int | None = None,
                 jitter=random.random):
        for name, value in (("rpm", rpm), ("tpm", tpm), ("max_inflight", max_inflight)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_inflight > DEFAULT_CONCURRENCY:
            raise ValueError(f"max_inflight must not exceed {DEFAULT_CONCURRENCY}")
        if not math.isfinite(warmup_s) or warmup_s < 0:
            raise ValueError("warmup_s must be finite and non-negative")
        for name, value in (("max_rpm", max_rpm), ("max_tpm", max_tpm)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        self.rpm, self.tpm = min(rpm, MAX_RPM), tpm
        self._adaptive = adaptive
        self._ceiling_rpm = min(MAX_RPM, max_rpm if max_rpm is not None else (
            MAX_RPM if adaptive and rpm == DEFAULT_RPM else self.rpm))
        self._ceiling_tpm = max_tpm if max_tpm is not None else (
            MAX_TPM if adaptive and tpm == DEFAULT_TPM else tpm)
        self.rpm = min(self.rpm, self._ceiling_rpm)
        self.tpm = min(self.tpm, self._ceiling_tpm)
        self._target_rpm = float(self.rpm)
        self._target_tpm = float(self.tpm)
        self._max_inflight = max_inflight
        self._inflight_limit = max_inflight
        self._clock, self._sleep = clock, sleep
        self._jitter = jitter
        self._slots = asyncio.Semaphore(max_inflight)
        self._lock = asyncio.Lock()
        self.dispatch_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._requests_second: deque[float] = deque()
        self._requests_minute: deque[float] = deque()
        self._records: deque[Reservation | _TokenCharge] = deque()
        self._outstanding: dict[int, Reservation] = {}
        self._pending = 0
        self._wait_reasons: dict[str, int] = {}
        self._peak_second = 0
        self._total_requests = 0
        self._total_successes = 0
        self._total_limited = 0
        self._next_send = 0.0
        self._last_send = None
        self._warm_started = clock()
        self._warm_start_on_send = True
        self._idle_since = self._warm_started
        self._warmup_s = warmup_s
        self._cooldown_until = 0.0
        self._last_limit_kind = None
        self._congestion_events = 0
        self._healthy_started = clock()
        self._healthy_successes = 0
        self._latencies: deque[float] = deque(maxlen=60)
        self._output_estimates = {"text": 8192.0, "vision": 8192.0}
        self._output_samples = {kind: deque(maxlen=20) for kind in ("text", "vision")}
        self._input_scale = {"text": 1.0, "vision": 1.0}

    @property
    def _scale(self):
        """Compatibility diagnostic: request target relative to initial RPM."""
        return self._target_rpm / self.rpm

    def estimate(self, input_tokens: int, kind: str = "text") -> int:
        return self.estimate_input(input_tokens, kind) + math.ceil(self._output_estimates[kind])

    def estimate_input(self, input_tokens: int, kind: str = "text") -> int:
        if kind not in self._input_scale:
            raise ValueError("kind must be text or vision")
        return math.ceil(input_tokens * self._input_scale[kind])

    def _prune(self, now: float):
        # Strict boundary: an attempt exactly one second old still counts.
        for records, window in ((self._requests_second, 1), (self._requests_minute, 60)):
            while records and records[0] < now - window:
                records.popleft()
        self._records = deque(record for record in self._records
                              if isinstance(record, Reservation) or record.sent >= now - 60)

    def _notify(self):
        # Rotate instead of clearing an Event shared with other waiters. A finish
        # between the lock release and wait registration must not lose its wakeup.
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def _wait(self, event: asyncio.Event, delay: float | None, reason: str):
        self._wait_reasons[reason] = self._wait_reasons.get(reason, 0) + 1
        signal = timer = None
        try:
            if delay is None:
                await event.wait()
            else:
                signal = asyncio.create_task(event.wait())
                timer = asyncio.create_task(self._sleep(max(DISPATCH_GUARD_S, delay)))
                await asyncio.wait({signal, timer}, return_when=asyncio.FIRST_COMPLETED)
                if timer.done():
                    timer.result()
        finally:
            self._wait_reasons[reason] -= 1
            children = [task for task in (signal, timer) if task is not None]
            for child in children:
                child.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)

    def _maybe_recover(self, now: float):
        # Recovery requires successful work and a real backlog; idle time alone
        # cannot turn the high-utilization mode on or erase a limit event.
        if (self._pending == 0 or now < self._cooldown_until
                or now - self._healthy_started < 60 or self._healthy_successes < 30):
            return
        if len(self._latencies) >= 30:
            samples = list(self._latencies)
            middle = len(samples) // 2
            earlier, recent = sorted(samples[:middle]), sorted(samples[middle:])
            if recent[int((len(recent) - 1) * .9)] > max(.001, earlier[int((len(earlier) - 1) * .9)]) * 1.25:
                return
        ceiling_rpm = self._ceiling_rpm if self._adaptive else self.rpm
        ceiling_tpm = self._ceiling_tpm if self._adaptive else self.tpm
        self._target_rpm = min(ceiling_rpm, self._target_rpm + 15)
        self._target_tpm = min(ceiling_tpm, self._target_tpm + 25_000)
        self._inflight_limit = min(self._max_inflight, self._inflight_limit + 8)
        self._healthy_started, self._healthy_successes = now, 0
        self._congestion_events = max(0, self._congestion_events - 1)

    def _admission_delay(self, tokens: int, now: float) -> tuple[float | None, str | None]:
        delays = [(max(0.0, self._next_send - now), "pacing"),
                  (max(0.0, self._cooldown_until - now), "cooldown")]
        if len(self._outstanding) >= self._inflight_limit:
            return None, "inflight"
        if len(self._requests_second) >= SECOND_REQUEST_LIMIT:
            delays.append((self._requests_second[0] + 1 + DISPATCH_GUARD_S - now, "second_window"))
        if len(self._requests_minute) >= max(1, math.floor(self._target_rpm)):
            index = len(self._requests_minute) - max(1, math.floor(self._target_rpm))
            delays.append((self._requests_minute[index] + 60 + DISPATCH_GUARD_S - now, "minute_window"))
        total = sum(record.tokens for record in self._records)
        # A reduction must not deadlock one request that fits the configured
        # account budget. Such a large request can run alone, at the slower token
        # pacing interval; it never bypasses the configured hard token ceiling.
        budget = max(tokens, self._target_tpm)
        if total + tokens > budget:
            released = 0
            expires = sorted((record.sent + 60 + DISPATCH_GUARD_S, record.tokens)
                             for record in self._records if isinstance(record, _TokenCharge))
            for expiry, amount in expires:
                released += amount
                if total + tokens - released <= budget:
                    delays.append((expiry - now, "token_budget"))
                    break
            else:
                return None, "token_budget"
        delay, reason = max(delays, key=lambda entry: entry[0])
        return delay, reason if delay > 0 else None

    def _begin_waiting(self) -> None:
        now = self._clock()
        if self._idle_since is not None:
            if self._last_send is None or now - self._idle_since > 30:
                self._warm_start_on_send = True
            self._idle_since = None
        self._pending += 1

    def _mark_idle(self) -> None:
        if (not self._pending and not self._outstanding and not self.dispatch_lock.locked()
                and self._idle_since is None):
            self._idle_since = self._clock()

    def _end_waiting(self) -> None:
        self._pending -= 1
        self._mark_idle()

    def release_dispatch(self) -> None:
        self.dispatch_lock.release()
        self._mark_idle()

    async def wait_for_dispatch(self) -> None:
        """Queue one connection candidate without opening an idle socket."""
        self._begin_waiting()
        self._wait_reasons["pacing"] = self._wait_reasons.get("pacing", 0) + 1
        try:
            with admission_wait(), timing_span("request_wait"):
                await self.dispatch_lock.acquire()
        finally:
            self._end_waiting()
            self._wait_reasons["pacing"] -= 1

    async def wait_until_ready(self, tokens: int | Callable[[], int]) -> None:
        """Wait before connecting, without reserving or counting an HTTP send.

        The transport holds dispatch_lock through this wait and the real header
        admission. Responses remain concurrent and can settle their reservations.
        The final acquire still rechecks every budget after pool/connect waits.
        """
        # A queued payload's estimate can shrink when earlier usage arrives.
        # Re-evaluate after every wakeup instead of freezing an inflated estimate
        # while holding the next connection turn.
        estimate = tokens if callable(tokens) else lambda: tokens
        self._begin_waiting()
        with admission_wait(), timing_span("request_wait"):
            try:
                while True:
                    async with self._lock:
                        now = self._clock()
                        current_tokens = estimate()
                        self._validate_tokens(current_tokens)
                        self._prune(now)
                        self._maybe_recover(now)
                        delay, reason = self._admission_delay(current_tokens, now)
                        if delay is not None and delay <= 0:
                            return
                        changed = self._changed
                    await self._wait(changed, delay, reason)
            finally:
                self._end_waiting()

    def _validate_tokens(self, tokens: int) -> None:
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise ValueError("tokens must be a positive integer")
        if tokens > self._ceiling_tpm:
            raise ValueError(f"单次预估 {tokens} Token 超过本地每分钟预算 {self._ceiling_tpm}，请检查输入大小")

    async def acquire(self, tokens: int, *, input_proxy: int = 0, kind: str = "text",
                      input_tokens: int | None = None, reestimate: bool = False) -> Reservation:
        self._validate_tokens(tokens)
        if kind not in self._input_scale:
            raise ValueError("kind must be text or vision")
        if input_tokens is not None and (isinstance(input_tokens, bool)
                or not isinstance(input_tokens, int) or not 0 <= input_tokens <= tokens):
            raise ValueError("input_tokens must be an integer in 0..tokens")
        self._begin_waiting()
        slot = False
        with admission_wait(), timing_span("request_wait"):
            try:
                self._wait_reasons["inflight"] = self._wait_reasons.get("inflight", 0) + 1
                try:
                    await self._slots.acquire()
                    slot = True
                finally:
                    self._wait_reasons["inflight"] -= 1
                while True:
                    async with self._lock:
                        now = self._clock()
                        if reestimate:
                            tokens = self.estimate(input_proxy, kind)
                            self._validate_tokens(tokens)
                            if input_tokens is not None:
                                input_tokens = min(tokens, self.estimate_input(input_proxy, kind))
                        self._prune(now)
                        self._maybe_recover(now)
                        delay, reason = self._admission_delay(tokens, now)
                        if delay is not None and delay <= 0:
                            if self._warm_start_on_send:
                                # Connection setup and local queue waits must
                                # not use up the first actual sends' warmup.
                                self._warm_started = now
                                self._warm_start_on_send = False
                            warm = (min(1.0, .25 + .75 * (now - self._warm_started) / self._warmup_s)
                                    if self._warmup_s else 1.0)
                            interval = max(60 / self._target_rpm, tokens * 60 / self._target_tpm) / warm
                            # Based on actual time, never on an old planned slot.
                            self._next_send = now + interval + DISPATCH_GUARD_S
                            self._last_send = now
                            record = Reservation(now, tokens, input_proxy, kind, owner=self)
                            record.initial_tokens = tokens
                            record.input_tokens = input_tokens or 0
                            record.interval = interval + DISPATCH_GUARD_S
                            self._records.append(record)
                            self._outstanding[id(record)] = record
                            self._requests_second.append(now)
                            self._requests_minute.append(now)
                            self._total_requests += 1
                            self._peak_second = max(self._peak_second, len(self._requests_second))
                            return record
                        changed = self._changed
                    await self._wait(changed, delay, reason)
            except BaseException:
                if slot:
                    self._slots.release()
                    self._notify()
                raise
            finally:
                self._end_waiting()

    def headers_sent(self, record: Reservation):
        """Fence a serialized header write at its completion (or failure).

        The transport holds dispatch_lock from acquire through this call. Socket
        writes may yield after httpcore's start trace; moving the timestamp later
        keeps that delay from accumulating send permits or compressing a window.
        This does not release the response's in-flight reservation.
        """
        if record.owner is not self:
            raise ValueError("reservation belongs to a different controller")
        if record.headers_fenced:
            return
        record.headers_fenced = True
        now = self._clock()
        for timestamps in (self._requests_second, self._requests_minute):
            try:
                timestamps.remove(record.sent)
            except ValueError:
                pass
            timestamps.append(now)
        record.sent = now
        self._next_send = max(self._next_send, now + record.interval)
        self._last_send = now
        self._prune(now)
        self._peak_second = max(self._peak_second, len(self._requests_second))
        self._notify()

    def confirm_input(self, record: Reservation) -> None:
        """Start input's minute window only once the server acknowledges it.

        Bailian pre-debits input on receipt and settles output at completion.
        Using successful response headers is later than receipt and therefore
        conservative. Slow output must not retain old input for its full lifetime.
        No-acknowledgement requests retain the complete reservation.
        """
        if record.owner is not self:
            raise ValueError("reservation belongs to a different controller")
        if record.finished or record.input_confirmed_at is not None or not record.input_tokens:
            return
        now = self._clock()
        record.input_confirmed_at = now
        record.input_charge = _TokenCharge(now, record.input_tokens)
        record.tokens -= record.input_tokens
        self._records.append(record.input_charge)
        self._prune(now)
        self._notify()

    @staticmethod
    def _limit_kind(error: BaseException | None) -> str | None:
        if error is None:
            return None
        status = getattr(error, "status_code", None)
        details = [str(error)]

        def include_code_and_message(body):
            # SDK/stream errors may only include a generic message in str(error).
            # Read provider error fields, never inspect echoed prompts for labels.
            if isinstance(body, dict):
                for key in ("code", "message", "type"):
                    if isinstance(body.get(key), str):
                        details.append(body[key])
                if isinstance(body.get("error"), dict):
                    include_code_and_message(body["error"])
                elif isinstance(body.get("error"), str):
                    details.append(body["error"])

        include_code_and_message(getattr(error, "body", None))
        detail = " ".join(details).lower()
        if status == 503:
            return "congestion"
        if any(term in detail for term in ("limit_burst", "increase rate", "burst rate", "burstquota",
                                          "rate increased too quickly")):
            return "burst"
        if any(term in detail for term in ("limit_tokens", "token rate", "tokens per", "token limit", "tpm", "tps",
                                          "allocated quota exceeded", "allocationquota")):
            return "token"
        if any(term in detail for term in ("limit_requests", "request rate", "requests per", "rpm", "rps", "ratequota")):
            return "request"
        if status == 429 or any(term in detail for term in ("throttling", "rate limit", "rate_limit")):
            return "unknown"
        return None

    def _feedback(self, error: BaseException | None, now: float):
        kind = self._limit_kind(error)
        if kind is None:
            if error is None:
                self._total_successes += 1
                if now >= self._cooldown_until:
                    self._healthy_successes += 1
            else:
                self._healthy_started, self._healthy_successes = now, 0
            return
        self._total_limited += 1
        self._last_limit_kind = kind
        new_wave = now >= self._cooldown_until
        if new_wave:
            self._congestion_events += 1
            if kind in {"request", "unknown", "congestion"}:
                self._target_rpm = max(min(1.0, self.rpm), self._target_rpm * .5)
            if kind in {"token", "unknown"}:
                self._target_tpm = max(1.0, self._target_tpm * .5)
            if kind in {"token", "congestion"}:
                self._inflight_limit = max(1, self._inflight_limit // 2)
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        wait = min(60.0, 5.0 * 2 ** min(4, max(0, self._congestion_events - 1)))
        wait += max(0.0, min(1.0, self._jitter()))
        if retry_after:
            try:
                parsed = float(retry_after)
            except (ValueError, TypeError):
                try:
                    parsed = parsedate_to_datetime(retry_after).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    parsed = 0.0
            if math.isfinite(parsed):
                wait = max(wait, parsed)
        self._cooldown_until = max(self._cooldown_until, now + wait)
        self._warm_started = self._cooldown_until
        self._healthy_started, self._healthy_successes = self._cooldown_until, 0
        if new_wave:
            log_event("请求调度", "限流降速", details={"类型": kind, "冷却秒数": wait,
                      "每秒请求目标": self._target_rpm / 60, "Token分钟目标": self._target_tpm})

    def finish(self, record: Reservation, usage=None, error: BaseException | None = None):
        """Settle one attempt exactly once, including errors and cancellations."""
        if record.owner is not self:
            raise ValueError("reservation belongs to a different controller")
        if record.finished:
            return
        record.finished = True
        self._outstanding.pop(id(record), None)
        try:
            now = self._clock()
            def value(key):
                raw = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
                return raw if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
                               and math.isfinite(raw) and raw >= 0) else None
            prompt, completion = value("prompt_tokens"), value("completion_tokens")
            total = value("total_tokens")
            if total is None and prompt is not None and completion is not None:
                total = prompt + completion
            self._records = deque(item for item in self._records
                                  if item is not record and item is not record.input_charge)
            if total is not None:
                if prompt is not None and completion is not None:
                    # Input is sent once; output can be generated much later.
                    input_at = record.input_confirmed_at if record.input_confirmed_at is not None else record.sent
                    self._records.append(_TokenCharge(input_at, math.ceil(prompt)))
                    self._records.append(_TokenCharge(now, math.ceil(max(completion, total - prompt))))
                else:
                    self._records.append(_TokenCharge(now, math.ceil(total)))
                if total > self._ceiling_tpm:
                    self._cooldown_until = max(self._cooldown_until, now + 60)
            else:
                # No usage is not evidence of no server work. Keep the estimate
                # for another minute after a disconnect, cancellation or error.
                self._records.append(_TokenCharge(now, record.initial_tokens or record.tokens))
            if completion is not None:
                samples = self._output_samples[record.kind]
                samples.append(completion)
                # Wait for three observations before reducing the cold estimate;
                # then retain the largest of the last 20 with 25% headroom. A
                # large response increases it immediately. A 0.1 EWMA previously
                # needed dozens of completed responses to free phantom output.
                floor = 8192 if len(samples) < 3 else 1024
                self._output_estimates[record.kind] = max(floor, max(samples) * 1.25)
            if prompt is not None and record.input_proxy:
                observed = prompt / record.input_proxy * 1.2
                previous = self._input_scale[record.kind]
                self._input_scale[record.kind] = max(.25, observed, previous * .9 + observed * .1)
            if error is None:
                self._latencies.append(max(0.0, now - record.sent))
            self._feedback(error, now)
            self._prune(now)
        finally:
            self._slots.release()
            self._notify()
            self._mark_idle()

    def snapshot(self) -> dict:
        """Public diagnostics; no keys, URLs, prompts or response bodies."""
        now = self._clock()
        self._prune(now)
        reserved = sum(record.tokens for record in self._outstanding.values())
        used = sum(record.tokens for record in self._records if isinstance(record, _TokenCharge))
        input_pending = sum(record.input_tokens for record in self._outstanding.values()
                            if record.input_confirmed_at is None)
        input_window = sum(record.input_charge.tokens for record in self._outstanding.values()
                           if record.input_charge is not None and record.input_charge.sent >= now - 60)
        reasons = ("cooldown", "token_budget", "second_window", "minute_window", "inflight", "pacing")
        reason = next((name for name in reasons if self._wait_reasons.get(name, 0)), None)
        if self._pending and now < self._cooldown_until:
            reason = "cooldown"
        return {
            "scope": "process_event_loop", "hard_second_limit": SECOND_REQUEST_LIMIT,
            "requests_last_second": len(self._requests_second),
            "requests_last_minute": len(self._requests_minute),
            "peak_requests_last_second": self._peak_second,
            "inflight": len(self._outstanding), "max_inflight": self._max_inflight,
            "inflight_limit": self._inflight_limit, "pending": self._pending,
            "reserved_tokens": reserved, "token_usage": used,
            "input_pending_tokens": input_pending, "input_tokens_window": input_window,
            "output_reserved_tokens": reserved - input_pending,
            "token_estimated_total": reserved + used, "token_budget": math.floor(self._target_tpm),
            "target_rps": self._target_rpm / 60, "request_budget": math.floor(self._target_rpm),
            "cooldown_remaining": max(0.0, self._cooldown_until - now),
            "wait_reason": reason, "last_limit_kind": self._last_limit_kind,
            "high_utilization_enabled": self._adaptive,
            "total_requests": self._total_requests, "total_successes": self._total_successes,
            "total_limited": self._total_limited,
        }


def _configured_throttle() -> RequestThrottle:
    adaptive_raw = os.getenv("AUTO_EVAL_BAILIAN_ADAPTIVE", "false").strip().lower()
    if adaptive_raw not in {"true", "false", "1", "0"}:
        raise ValueError("AUTO_EVAL_BAILIAN_ADAPTIVE must be true/false or 1/0")
    adaptive = adaptive_raw in {"true", "1"}

    def budget(name, default, maximum):
        raw = os.getenv(name)
        try:
            result = default if raw is None else int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer in 1..{maximum}") from exc
        if not 1 <= result <= maximum:
            raise ValueError(f"{name} must be an integer in 1..{maximum}")
        return result

    ceiling_rpm = budget("AUTO_EVAL_BAILIAN_RPM", MAX_RPM if adaptive else DEFAULT_RPM, MAX_RPM)
    ceiling_tpm = budget("AUTO_EVAL_BAILIAN_TPM", MAX_TPM if adaptive else DEFAULT_TPM, MAX_TPM)
    if not adaptive and (ceiling_rpm > DEFAULT_RPM or ceiling_tpm > DEFAULT_TPM):
        raise ValueError("Budgets above 480 RPM / 800000 TPM require AUTO_EVAL_BAILIAN_ADAPTIVE=true")
    return RequestThrottle(rpm=min(DEFAULT_RPM, ceiling_rpm), tpm=min(DEFAULT_TPM, ceiling_tpm),
                           adaptive=adaptive, max_rpm=ceiling_rpm, max_tpm=ceiling_tpm)


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
        controllers[MODEL] = _configured_throttle()
    return controllers[MODEL]
