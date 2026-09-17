"""Deterministic dispatch-window/accounting checks; no model or network calls."""
import asyncio
from types import SimpleNamespace

import pytest

from auto_eval.request_throttle import (
    DEFAULT_TPM, DISPATCH_GUARD_S, MAX_RPM, RequestThrottle,
    _configured_throttle, admission_wait, wait_for_active,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        assert delay > 0
        self.now += delay


def limiter(**kwargs):
    clock = Clock()
    return RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=0, jitter=lambda: 0, **kwargs), clock


class LimitError(Exception):
    def __init__(self, message, status=429, retry_after=None):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers={} if retry_after is None else {"Retry-After": retry_after})


@pytest.mark.asyncio
async def test_second_hard_window_survives_disabled_pacing_and_huge_rpm_override():
    throttle, clock = limiter(rpm=60_000, tpm=10**9)
    sent = []
    for _ in range(1200):
        # Deliberately break soft pacing: independent windows still protect us.
        throttle._next_send = 0
        record = await throttle.acquire(1)
        sent.append(record.sent)
        throttle.finish(record, {"total_tokens": 0})
    for index, timestamp in enumerate(sent):
        if index >= 9:
            assert timestamp - sent[index - 9] > 1
        if index >= MAX_RPM:
            assert timestamp - sent[index - MAX_RPM] > 60
    assert throttle.snapshot()["peak_requests_last_second"] == 9


@pytest.mark.asyncio
async def test_strict_boundary_and_pause_do_not_accumulate_permits():
    throttle, clock = limiter()
    first = await throttle.acquire(1)
    throttle.finish(first, {"total_tokens": 0})
    clock.now = 100
    next_record = await throttle.acquire(1)
    last = await throttle.acquire(1)
    assert last.sent - next_record.sent >= .125 + DISPATCH_GUARD_S - 1e-9
    throttle.finish(next_record)
    throttle.finish(last)


@pytest.mark.asyncio
async def test_large_request_pacing_does_not_restart_warmup_while_waiting():
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=15)
    first = await throttle.acquire(500_000)
    throttle.finish(first, {"total_tokens": 10})
    second = await throttle.acquire(500_000)
    throttle.finish(second, {"total_tokens": 10})
    third = await throttle.acquire(500_000)
    assert first.sent == 0  # Warmup never adds a delay before the first send.
    assert second.sent - first.sent == pytest.approx(37.5 + DISPATCH_GUARD_S)
    assert third.sent - second.sent == pytest.approx(37.5 + DISPATCH_GUARD_S)
    throttle.finish(third)


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,expected_gap", [(50_000, 3.75), (100_000, 7.5), (150_000, 11.25)])
async def test_startup_large_requests_use_token_pacing_without_extra_warmup(tokens, expected_gap):
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=15)
    first = await throttle.acquire(tokens)
    assert first.sent == clock.now == 0
    # The second call must start while the first response is still pending.
    # These fixed workloads should not wait 15/30/45 seconds at startup.
    second = await throttle.acquire(tokens)
    assert second.sent - first.sent == pytest.approx(expected_gap + DISPATCH_GUARD_S)
    assert throttle.snapshot()["inflight"] == 2
    throttle.finish(first, {"total_tokens": tokens})
    throttle.finish(second, {"total_tokens": tokens})


@pytest.mark.asyncio
async def test_idle_restart_does_not_multiply_large_request_token_interval():
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=15)
    first = await throttle.acquire(100_000)
    throttle.finish(first, {"total_tokens": 10})
    clock.now += 31  # Truly idle long enough to restart request-frequency warmup.
    second = await throttle.acquire(100_000)
    assert second.sent == 31
    third = await throttle.acquire(100_000)
    assert third.sent - second.sent == pytest.approx(7.5 + DISPATCH_GUARD_S)
    throttle.finish(second)
    throttle.finish(third)


@pytest.mark.asyncio
async def test_faster_startup_still_waits_for_full_token_budget_to_settle():
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=15)
    first = await throttle.acquire(DEFAULT_TPM)
    clock.now = 61  # Soft pacing and HTTP windows have elapsed, tokens have not.
    waiting = asyncio.create_task(throttle.acquire(150_000))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert throttle.snapshot()["wait_reason"] == "token_budget"
    assert throttle.snapshot()["total_requests"] == 1
    throttle.finish(first, {"total_tokens": 600_000})
    second = await asyncio.wait_for(waiting, .5)
    assert second.sent == 61
    assert throttle.snapshot()["token_estimated_total"] == 750_000
    throttle.finish(second)


@pytest.mark.asyncio
async def test_warmup_restarts_only_after_finished_work_is_truly_idle():
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=15)
    first = await throttle.acquire(100)
    clock.now = 90  # Long-running responses are active, not idle.
    throttle.finish(first, {"total_tokens": 10})
    second = await throttle.acquire(100)
    assert second.interval == pytest.approx(.125 + DISPATCH_GUARD_S)
    throttle.finish(second, {"total_tokens": 10})
    clock.now += 31  # No queued, preparing-to-send or in-flight work.
    third = await throttle.acquire(100)
    assert third.interval == pytest.approx(.5 + DISPATCH_GUARD_S)
    throttle.finish(third)


@pytest.mark.asyncio
async def test_slow_connection_does_not_consume_startup_or_idle_warmup():
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=15)
    for connection_delay in (20, 40):
        await throttle.wait_for_dispatch()
        await throttle.wait_until_ready(100)
        clock.now += connection_delay  # Pool/TCP/TLS setup before any HTTP send.
        record = await throttle.acquire(100)
        assert record.interval == pytest.approx(.5 + DISPATCH_GUARD_S)
        throttle.headers_sent(record)
        throttle.release_dispatch()
        throttle.finish(record, {"total_tokens": 10})
        clock.now += 31


@pytest.mark.asyncio
async def test_header_completion_fences_socket_yield_without_freeing_response_slot():
    throttle, clock = limiter()
    async with throttle.dispatch_lock:
        first = await throttle.acquire(1)
        clock.now = 40
        throttle.headers_sent(first)
        throttle.headers_sent(first)
    snapshot = throttle.snapshot()
    assert snapshot["inflight"] == snapshot["requests_last_second"] == 1
    assert snapshot["total_requests"] == 1
    second = await throttle.acquire(1)
    assert second.sent >= 40.126
    throttle.finish(first)
    throttle.finish(second)


@pytest.mark.asyncio
async def test_unfinished_token_reservation_never_expires_and_settlement_wakes_waiter():
    throttle, clock = limiter(tpm=100)
    first = await throttle.acquire(100)
    clock.now = 90
    waiting = asyncio.create_task(throttle.acquire(20))
    await asyncio.sleep(0)
    assert not waiting.done()
    assert not throttle._lock.locked()
    snapshot = throttle.snapshot()
    assert snapshot["reserved_tokens"] == 100
    assert snapshot["requests_last_minute"] == 0
    assert snapshot["wait_reason"] == "token_budget"
    throttle.finish(first, {"prompt_tokens": 80, "completion_tokens": 10})
    second = await asyncio.wait_for(waiting, .5)
    assert second.sent == 90
    assert throttle.snapshot()["total_requests"] == 2
    assert throttle.snapshot()["token_usage"] == 10
    throttle.finish(second)


@pytest.mark.asyncio
async def test_output_settlement_does_not_create_phantom_http_requests():
    throttle, clock = limiter(rpm=1)
    first = await throttle.acquire(100)
    clock.now = 90
    throttle.finish(first, {"prompt_tokens": 80, "completion_tokens": 30})
    assert throttle.snapshot()["requests_last_minute"] == 0
    assert throttle.snapshot()["token_usage"] == 30
    second = await throttle.acquire(100)
    assert second.sent == 90
    assert throttle.snapshot()["requests_last_minute"] == 1
    throttle.finish(second)


@pytest.mark.asyncio
async def test_streamed_output_keeps_own_sixty_second_window_without_double_charge():
    throttle, clock = limiter()
    first = await throttle.acquire(100)
    clock.now = 30
    throttle.finish(first, {"prompt_tokens": 70, "completion_tokens": 20})
    assert throttle.snapshot()["token_estimated_total"] == 90
    clock.now = 61
    assert throttle.snapshot()["token_estimated_total"] == 20
    clock.now = 90
    assert throttle.snapshot()["token_estimated_total"] == 20
    clock.now += .001
    assert throttle.snapshot()["token_estimated_total"] == 0


@pytest.mark.asyncio
async def test_duplicate_finish_and_unknown_usage_cancellation_preserve_accounting():
    throttle, clock = limiter(max_inflight=2)
    first = await throttle.acquire(100)
    clock.now = 90
    throttle.finish(first, error=asyncio.CancelledError())
    throttle.finish(first, {"total_tokens": 0})
    assert throttle._slots._value == 2
    assert throttle.snapshot()["reserved_tokens"] == 0
    assert throttle.snapshot()["token_usage"] == 100
    clock.now = 151
    assert throttle.snapshot()["token_usage"] == 0


@pytest.mark.asyncio
async def test_many_cancelled_waiters_release_slots_and_events_without_dispatching():
    throttle = RequestThrottle(max_inflight=4, warmup_s=0)
    first = await throttle.acquire(1)
    throttle._next_send = throttle._clock() + 100
    waiters = [asyncio.create_task(throttle.acquire(1)) for _ in range(20)]
    await asyncio.sleep(.01)
    for waiter in waiters:
        waiter.cancel()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    throttle.finish(first)
    assert throttle._slots._value == 4
    snapshot = throttle.snapshot()
    assert snapshot["pending"] == snapshot["inflight"] == 0
    assert snapshot["total_requests"] == 1
    assert snapshot["wait_reason"] is None
    assert not throttle._lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("message,status,kind,rpm,tpm,inflight", [
    ("limit_requests", 429, "request", 240, 800_000, 128),
    ("limit_tokens", 429, "token", 480, 400_000, 64),
    ("limit_burst_rate", 429, "burst", 480, 800_000, 128),
    ("busy", 503, "congestion", 240, 800_000, 64),
    ("limited", 429, "unknown", 240, 400_000, 128),
])
async def test_feedback_distinguishes_limits_and_deduplicates_response_wave(message, status, kind, rpm, tpm, inflight):
    throttle, clock = limiter()
    first, second = await throttle.acquire(1), await throttle.acquire(1)
    error = LimitError(message, status, "20")
    throttle.finish(first, error=error)
    throttle.finish(second, error=error)
    snapshot = throttle.snapshot()
    assert snapshot["last_limit_kind"] == kind
    assert snapshot["request_budget"] == rpm
    assert snapshot["token_budget"] == tpm
    assert snapshot["inflight_limit"] == inflight
    assert snapshot["cooldown_remaining"] >= 20
    assert snapshot["total_limited"] == 2
    third = await throttle.acquire(1)
    assert third.sent >= 20
    throttle.finish(third)


@pytest.mark.parametrize("body,expected", [
    ({"code": "Throttling.AllocationQuota"}, "token"),
    ({"message": "Allocated quota exceeded"}, "token"),
    ({"error": {"message": "Request rate increased too quickly"}}, "burst"),
    ({"error": {"code": "Throttling.BurstQuota"}}, "burst"),
    ({"error": {"code": "Throttling.RateQuota"}}, "request"),
    ({"messages": [{"content": "token rate exceeded"}]}, "unknown"),
])
def test_provider_error_code_and_nested_body_classification(body, expected):
    error = LimitError("provider error")
    error.body = body
    assert RequestThrottle._limit_kind(error) == expected


@pytest.mark.asyncio
async def test_adaptive_mode_requires_successes_time_and_backlog_and_stays_below_caps():
    throttle, clock = limiter(adaptive=True)
    clock.now = 120
    assert throttle.snapshot()["request_budget"] == 480  # Idle never upgrades.
    for _ in range(29):
        record = await throttle.acquire(1)
        throttle.finish(record, {"total_tokens": 1})
    assert throttle.snapshot()["request_budget"] == 480
    record = await throttle.acquire(1)
    throttle.finish(record, {"total_tokens": 1})
    record = await throttle.acquire(1)
    assert throttle.snapshot()["request_budget"] == 495
    assert throttle.snapshot()["token_budget"] == 825_000
    throttle.finish(record, {"total_tokens": 1})
    for _ in range(5):
        clock.now += 61
        for _ in range(30):
            record = await throttle.acquire(1)
            throttle.finish(record, {"total_tokens": 1})
    assert throttle.snapshot()["request_budget"] == 540
    assert throttle.snapshot()["token_budget"] == 900_000


@pytest.mark.asyncio
async def test_recovery_after_request_limit_needs_full_healthy_interval_and_backlog():
    throttle, clock = limiter()
    first = await throttle.acquire(1)
    throttle.finish(first, error=LimitError("limit_requests"))
    assert throttle.snapshot()["target_rps"] == 4
    for _ in range(30):
        record = await throttle.acquire(1)
        throttle.finish(record, {"total_tokens": 1})
    assert clock.now < 60
    assert throttle.snapshot()["target_rps"] == 4
    clock.now = 70
    assert throttle.snapshot()["target_rps"] == 4  # No waiting work yet.
    record = await throttle.acquire(1)
    assert throttle.snapshot()["target_rps"] == 4.25
    assert throttle.snapshot()["token_budget"] == 800_000
    throttle.finish(record)


@pytest.mark.asyncio
async def test_default_budget_cannot_auto_upgrade_and_text_calibration_does_not_change_vision():
    throttle, clock = limiter()
    vision_before = throttle.estimate(100, "vision")
    for _ in range(35):
        record = await throttle.acquire(100, input_proxy=100, kind="text")
        throttle.finish(record, {"prompt_tokens": 10, "completion_tokens": 1})
    clock.now += 61
    record = await throttle.acquire(100)
    assert throttle.snapshot()["request_budget"] == 480
    assert throttle.snapshot()["token_budget"] == DEFAULT_TPM
    assert throttle.estimate(100, "vision") == vision_before
    throttle.finish(record)


@pytest.mark.parametrize("variable,value", [
    ("AUTO_EVAL_BAILIAN_ADAPTIVE", "maybe"), ("AUTO_EVAL_BAILIAN_RPM", "541"),
    ("AUTO_EVAL_BAILIAN_RPM", "0"), ("AUTO_EVAL_BAILIAN_TPM", "900001"),
    ("AUTO_EVAL_BAILIAN_TPM", "nan"), ("AUTO_EVAL_BAILIAN_RPM", "540"),
])
def test_environment_rejects_invalid_or_unapproved_high_budgets(monkeypatch, variable, value):
    for name in ("AUTO_EVAL_BAILIAN_ADAPTIVE", "AUTO_EVAL_BAILIAN_RPM", "AUTO_EVAL_BAILIAN_TPM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        _configured_throttle()


def test_explicit_low_account_limits_are_never_increased_by_adaptive_mode(monkeypatch):
    monkeypatch.setenv("AUTO_EVAL_BAILIAN_ADAPTIVE", "true")
    monkeypatch.setenv("AUTO_EVAL_BAILIAN_RPM", "120")
    monkeypatch.setenv("AUTO_EVAL_BAILIAN_TPM", "10000")
    throttle = _configured_throttle()
    assert throttle.rpm == throttle._ceiling_rpm == 120
    assert throttle.tpm == throttle._ceiling_tpm == 10000


@pytest.mark.asyncio
async def test_nested_active_deadlines_both_exclude_dispatch_wait():
    async def dispatch():
        with admission_wait():
            await asyncio.sleep(.08)
        await asyncio.sleep(.002)
        return "ok"

    async def per_http():
        return await wait_for_active(dispatch(), .025)

    assert await wait_for_active(per_http(), .04) == "ok"
    with pytest.raises(asyncio.TimeoutError):
        await wait_for_active(wait_for_active(asyncio.sleep(.08), .02), .1)


@pytest.mark.asyncio
async def test_confirmed_input_expires_while_long_output_remains_reserved():
    throttle, clock = limiter(tpm=100)
    first = await throttle.acquire(100, input_tokens=80)
    clock.now = 10
    throttle.confirm_input(first)
    throttle.confirm_input(first)
    snapshot = throttle.snapshot()
    assert snapshot["token_estimated_total"] == 100
    assert snapshot["reserved_tokens"] == snapshot["output_reserved_tokens"] == 20
    assert snapshot["input_tokens_window"] == 80
    clock.now = 70
    assert throttle.snapshot()["token_estimated_total"] == 100
    clock.now = 70.001
    assert throttle.snapshot()["token_estimated_total"] == 20
    # This request can use the released minute budget before the first output
    # completes; the old all-in-flight ledger would block indefinitely.
    second = await throttle.acquire(80, input_tokens=60)
    assert second.sent == 70.001
    assert throttle.snapshot()["token_estimated_total"] == 100
    throttle.finish(first, {"prompt_tokens": 80, "completion_tokens": 20})
    throttle.finish(second, {"prompt_tokens": 60, "completion_tokens": 20})


@pytest.mark.asyncio
async def test_split_input_without_server_acknowledgement_does_not_expire():
    throttle, clock = limiter()
    first = await throttle.acquire(100, input_tokens=80)
    clock.now = 1000
    snapshot = throttle.snapshot()
    assert snapshot["input_pending_tokens"] == 80
    assert snapshot["reserved_tokens"] == 100
    assert snapshot["input_tokens_window"] == 0
    throttle.finish(first)


@pytest.mark.asyncio
async def test_confirmed_input_settlement_uses_ack_window_without_double_charge():
    throttle, clock = limiter()
    first = await throttle.acquire(100, input_tokens=80)
    clock.now = 50
    throttle.confirm_input(first)
    clock.now = 70
    throttle.finish(first, {"prompt_tokens": 70, "completion_tokens": 10})
    assert throttle.snapshot()["token_estimated_total"] == 80
    clock.now = 110.001
    assert throttle.snapshot()["token_estimated_total"] == 10
    clock.now = 130.001
    assert throttle.snapshot()["token_estimated_total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("disconnect"), asyncio.CancelledError()])
async def test_unknown_usage_restores_full_conservative_charge_after_input_expiry(error):
    throttle, clock = limiter()
    first = await throttle.acquire(100, input_tokens=80)
    throttle.confirm_input(first)
    clock.now = 90
    assert throttle.snapshot()["token_estimated_total"] == 20
    throttle.finish(first, error=error)
    assert throttle.snapshot()["token_estimated_total"] == 100
    assert throttle.snapshot()["inflight"] == 0
    clock.now = 150.001
    assert throttle.snapshot()["token_estimated_total"] == 0


@pytest.mark.asyncio
async def test_output_calibration_waits_for_evidence_then_keeps_recent_upper_envelope():
    throttle, clock = limiter()
    for index in range(3):
        record = await throttle.acquire(1, kind="vision")
        throttle.finish(record, {"prompt_tokens": 0, "completion_tokens": 1000})
        assert throttle._output_estimates["vision"] == (8192 if index < 2 else 1250)
    record = await throttle.acquire(1, kind="vision")
    throttle.finish(record, {"prompt_tokens": 0, "completion_tokens": 10_000})
    assert throttle._output_estimates["vision"] == 12_500
    assert throttle._output_estimates["text"] == 8192
    for _ in range(19):
        record = await throttle.acquire(1, kind="vision")
        throttle.finish(record, {"prompt_tokens": 0, "completion_tokens": 100})
    assert throttle._output_estimates["vision"] == 12_500


@pytest.mark.asyncio
async def test_queued_preconnect_estimate_refreshes_after_usage_calibration():
    throttle, clock = limiter(tpm=10_000)
    held = await throttle.acquire(4000)
    samples = [await throttle.acquire(1) for _ in range(3)]
    clock.now = 10
    waiting = asyncio.create_task(throttle.wait_until_ready(lambda: throttle.estimate(1000)))
    await asyncio.sleep(0)
    assert not waiting.done()  # 4000 + initial 9192 cannot fit.
    for sample in samples:
        throttle.finish(sample, {"prompt_tokens": 0, "completion_tokens": 100})
    await asyncio.wait_for(waiting, .5)
    assert throttle.estimate(1000) == 2024
    assert throttle.snapshot()["total_requests"] == 4  # Readiness never counts sends.
    throttle.finish(held)


@pytest.mark.asyncio
async def test_header_admission_refreshes_estimate_after_usage_calibration():
    throttle, clock = limiter(tpm=10_000)
    held = await throttle.acquire(4000)
    samples = [await throttle.acquire(1) for _ in range(3)]
    clock.now = 10
    waiting = asyncio.create_task(throttle.acquire(
        throttle.estimate(1000), input_proxy=1000, input_tokens=1000, reestimate=True,
    ))
    await asyncio.sleep(0)
    assert not waiting.done()
    for sample in samples:
        throttle.finish(sample, {"prompt_tokens": 0, "completion_tokens": 100})
    record = await asyncio.wait_for(waiting, .5)
    assert record.tokens == record.initial_tokens == 2024
    assert record.input_tokens == 1000
    throttle.finish(record)
    throttle.finish(held)
