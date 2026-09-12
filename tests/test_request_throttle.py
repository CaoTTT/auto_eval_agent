"""Admission, accounting and throughput tests; never call a real model."""
import asyncio
import base64
import io
import heapq
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError, RateLimitError
from PIL import Image

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval import llm_stream
from auto_eval.preparation import preparation_limit, run_preparation
from auto_eval.request_throttle import (
    RequestThrottle, admission_wait, estimate_input_tokens, recommended_concurrency,
    shared_throttle, supports_bailian_pacing, wait_for_active,
)


def judge(**kwargs):
    return JudgeConfig(name="judge_2", model="qwen3.5-397b-a17b",
                       base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", **kwargs)


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
    return RequestThrottle(clock=clock, sleep=clock.sleep, warmup_s=0, **kwargs), clock


def test_only_bailian_target_model_gets_automatic_defaults():
    assert recommended_concurrency([judge()]) == 128
    for host in ["dashscope-intl.aliyuncs.com", "workspace.cn-beijing.maas.aliyuncs.com"]:
        assert supports_bailian_pacing(judge().model_copy(update={"base_url": f"https://{host}/v1"}))
    for update in [{"model": "qwen-plus"}, {"base_url": "https://api.siliconflow.cn/v1"},
                   {"base_url": "https://dashscope.aliyuncs.com.evil.example/v1"}]:
        assert recommended_concurrency([judge().model_copy(update=update)]) == 4


@pytest.mark.asyncio
async def test_keeps_sending_before_previous_responses_return():
    throttle, clock = limiter()
    records = [await throttle.acquire(100) for _ in range(16)]
    # No finish/release occurred: rate admission does not wait for a batch.
    assert [r.sent for r in records] == pytest.approx([i / 8 for i in range(16)])
    for record in records:
        throttle.finish(record)


@pytest.mark.asyncio
async def test_tpm_sliding_window_and_usage_settlement():
    throttle, clock = limiter(rpm=600, tpm=1000)
    first = await throttle.acquire(400)
    throttle.finish(first, {"prompt_tokens": 600, "completion_tokens": 100})
    second = await throttle.acquire(400)
    assert second.sent == 60  # Actual usage, not just RPM, blocks admission.
    throttle.finish(second, {"total_tokens": 100})
    third = await throttle.acquire(400)
    assert third.sent == 84
    throttle.finish(third)


@pytest.mark.asyncio
async def test_request_rolling_window_and_no_idle_burst():
    throttle, clock = limiter(rpm=8, tpm=1_000_000)
    records = [await throttle.acquire(1) for _ in range(9)]
    assert records[-1].sent >= 60
    for record in records:
        throttle.finish(record)
    clock.now += 600
    a, b = await throttle.acquire(1), await throttle.acquire(1)
    assert b.sent - a.sent >= 7.5
    throttle.finish(a)
    throttle.finish(b)


@pytest.mark.asyncio
async def test_warmup_restarts_after_idle():
    clock = Clock()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep)
    a, b = await throttle.acquire(1), await throttle.acquire(1)
    assert b.sent - a.sent == .5  # 2 RPS startup, gradually growing to 8.
    throttle.finish(a)
    throttle.finish(b)
    clock.now += 40
    c, d = await throttle.acquire(1), await throttle.acquire(1)
    assert d.sent - c.sent == .5
    throttle.finish(c)
    throttle.finish(d)


@pytest.mark.asyncio
async def test_shared_429_cooldown_honors_retry_after_and_recovers():
    throttle, clock = limiter()
    a, b = await throttle.acquire(1), await throttle.acquire(1)
    exc = RateLimitError("limited", response=httpx.Response(
        429, headers={"Retry-After": "20"}, request=httpx.Request("POST", "https://example.test")), body={})
    throttle.finish(a, error=exc)
    throttle.finish(b, error=exc)
    assert throttle._scale == .5  # An in-flight error wave halves only once.
    c = await throttle.acquire(1)
    assert c.sent >= 20
    clock.now += 11
    throttle.finish(c, {"total_tokens": 1})
    assert throttle._scale == .6


@pytest.mark.asyncio
async def test_long_stream_usage_is_not_lost_after_reservation_expires():
    throttle, clock = limiter()
    record = await throttle.acquire(100)
    clock.now = 90
    throttle.finish(record, {"prompt_tokens": 100, "completion_tokens": 500})
    assert throttle._records[-1].sent == 90
    assert throttle._records[-1].tokens == 500


@pytest.mark.asyncio
async def test_inflight_bound_cancelled_waiter_and_failed_request_release_slots():
    throttle = RequestThrottle(rpm=60000, tpm=10**9, max_inflight=1, warmup_s=0)
    first = await throttle.acquire(1)
    pending = asyncio.create_task(throttle.acquire(1))
    await asyncio.sleep(.01)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    throttle.finish(first, error=RuntimeError("failure"))
    next_record = await asyncio.wait_for(throttle.acquire(1), 1)
    throttle.finish(next_record)
    assert throttle._slots._value == 1


@pytest.mark.asyncio
async def test_cancel_during_pacing_does_not_leave_future_reservations():
    throttle = RequestThrottle()
    throttle._next_send = time.monotonic() + 100
    pending = asyncio.create_task(throttle.acquire(1))
    await asyncio.sleep(.01)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not throttle._records
    assert throttle._slots._value == 128
    assert not throttle._lock.locked()


@pytest.mark.asyncio
async def test_controllers_shared_across_clients_and_api_keys():
    assert shared_throttle(judge(api_key_env="KEY_A")) is shared_throttle(judge(api_key_env="KEY_B"))


def test_image_token_proxy_counts_geometry_not_base64():
    buf = io.BytesIO()
    Image.new("RGB", (320, 640)).save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    kwargs = {"messages": [{"content": [{"type": "text", "text": "你好"},
              {"type": "image_url", "image_url": {"url": url}}]}]}
    assert estimate_input_tokens(kwargs) == 256 + 6 + 200 + 256


@pytest.mark.asyncio
async def test_usage_calibrates_text_without_changing_visual_estimates():
    throttle, clock = limiter()
    initial = throttle.estimate(10000)
    record = await throttle.acquire(initial, input_proxy=10000)
    throttle.finish(record, {"prompt_tokens": 3000, "completion_tokens": 2000})
    assert throttle.estimate(10000) < initial
    assert throttle._input_scale["vision"] == 1
    assert throttle._input_scale["text"] >= .36


@pytest.mark.asyncio
async def test_virtual_throughput_benchmark(capsys):
    # Virtual 240-case run: 30 seconds/model call, 10K tokens/call.
    # The exact production limiter advances virtual time; no network or sleeps.
    class Simulation(Clock):
        def __init__(self):
            super().__init__()
            self.completions = []
            self.sequence = 0
            self.peak = 0
        async def sleep(self, delay):
            target = self.now + delay
            while self.completions and self.completions[0][0] <= target:
                self.now, _, record = heapq.heappop(self.completions)
                throttle.finish(record, {"prompt_tokens": 8000, "completion_tokens": 2000})
            self.now = target
        def submit(self, record):
            self.sequence += 1
            heapq.heappush(self.completions, (self.now + 30, self.sequence, record))
            self.peak = max(self.peak, len(self.completions))
    clock = Simulation()
    throttle = RequestThrottle(clock=clock, sleep=clock.sleep)
    sent = []
    for _ in range(240):
        record = await throttle.acquire(10000)
        sent.append(record.sent)
        clock.submit(record)
    await clock.sleep(30)
    original = 240 / 10 * 30
    assert clock.now < original / 3
    assert 10 < clock.peak <= 128
    for start in sent:
        assert sum(start <= t < start + 60 for t in sent) <= 80
    # Kept as reproducible benchmark evidence for the design document.
    with capsys.disabled():
        print(f"\nVirtual benchmark: old={original:.2f}s new={clock.now:.2f}s "
              f"speedup={original/clock.now:.2f}x peak={clock.peak}")


def test_api_exposes_recommended_capacity_without_exposing_provider_secrets(monkeypatch):
    from auto_eval.web import server
    monkeypatch.setattr(server, "cfg", lambda: AppConfig(judges=[judge(api_key_env="SECRET_NAME")]))
    public = server.api_config()["judges"][0]
    assert public["recommended_concurrency"] == 128
    assert public["request_pacing"] is True
    assert "api_key_env" not in public and "base_url" not in public


@pytest.mark.asyncio
async def test_rate_wait_excluded_but_execution_still_times_out():
    async def slow_admission():
        with admission_wait():
            await asyncio.sleep(.08)
        await asyncio.sleep(.005)
        return "ok"
    assert await wait_for_active(slow_admission(), timeout=.04) == "ok"
    with pytest.raises(asyncio.TimeoutError):
        await wait_for_active(asyncio.sleep(.1), timeout=.01)


@pytest.mark.asyncio
async def test_active_timeout_cancellation_reaps_child():
    finished = asyncio.Event()
    async def child():
        try:
            with admission_wait():
                await asyncio.sleep(100)
        finally:
            finished.set()
    job = asyncio.create_task(wait_for_active(child(), timeout=1))
    await asyncio.sleep(.01)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert finished.is_set()


@pytest.mark.asyncio
async def test_media_preparation_bounded_independently():
    active = peak = 0
    lock = threading.Lock()
    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.02)
        with lock:
            active -= 1
    with preparation_limit(asyncio.Semaphore(2)):
        await asyncio.gather(*(run_preparation(work, timeout=5) for _ in range(8)))
    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_error", ["usage", "network"])
async def test_every_actual_stream_attempt_passes_admission(monkeypatch, first_error):
    attempts = []
    response = SimpleNamespace(usage={"total_tokens": 10}, choices=[SimpleNamespace(
        message=SimpleNamespace(tool_calls=None), finish_reason="stop")])
    async def collect(client, kwargs, *, include_usage):
        attempts.append(kwargs)
        if len(attempts) == 1:
            if first_error == "usage":
                raise BadRequestError("stream_options not supported", body={}, response=httpx.Response(
                    400, request=httpx.Request("POST", "https://example.test")))
            raise httpx.RemoteProtocolError("disconnect")
        return response, [], {}
    monkeypatch.setattr(llm_stream, "_collect_stream", collect)
    throttle, clock = limiter()
    result = await llm_stream.stream_chat_completion(None, {"messages": [], "model": "fake"},
                                                    throttle=throttle, retry_base_s=0)
    assert result is response
    assert len(attempts) == len(throttle._records) == 2
    assert attempts[1]["extra_headers"]["X-DashScope-Wait-Timeout"] == "30"
    assert throttle._slots._value == 128


@pytest.mark.asyncio
async def test_stream_timeout_starts_after_admission_and_releases_slot(monkeypatch):
    throttle = RequestThrottle(warmup_s=0)
    throttle._next_send = time.monotonic() + .08
    async def collect(*args, **kwargs):
        await asyncio.sleep(.1)
    monkeypatch.setattr(llm_stream, "_collect_stream", collect)
    with pytest.raises(asyncio.TimeoutError):
        await wait_for_active(llm_stream.stream_chat_completion(None, {"messages": []},
            throttle=throttle, total_timeout_s=.02, max_attempts=1), timeout=.05)
    assert len(throttle._records) == 1  # It actually reached the network boundary.
    assert throttle._slots._value == 128


@pytest.mark.asyncio
async def test_real_runner_admits_more_than_ten_cases_and_keeps_manual_cap(monkeypatch):
    from auto_eval.web import runner
    from auto_eval.web.tasks import Task
    class Client:
        def __init__(self, cfg):
            pass
        async def aclose(self):
            pass
    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    cfg = AppConfig(judges=[judge()])
    async def run(capacity):
        active = peak = 0
        entered = asyncio.Event()
        release = asyncio.Event()
        async def evaluate(mode, idx, item, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == (12 if capacity is None else capacity):
                entered.set()
            try:
                await release.wait()
                return {"index": idx}
            finally:
                active -= 1
        monkeypatch.setattr(runner, "_eval_one", evaluate)
        task = Task(id="pacing-test", mode="compare", items=[
            {"query": str(i), "frames1": ["prepared"], "frames2": ["prepared"]} for i in range(12)
        ], options={} if capacity is None else {"concurrency": capacity})
        job = asyncio.create_task(runner._run(task, cfg))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            release.set()
            await asyncio.wait_for(job, 2)
        finally:
            release.set()
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        assert len(task.results) == 12
        return peak
    assert await run(None) == 12
    assert await run(3) == 3
