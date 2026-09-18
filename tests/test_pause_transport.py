"""Pause unsent HTTP work while allowing dispatched streams to settle."""
import asyncio

import pytest

from auto_eval.config import JudgeConfig
from auto_eval.judges.base import JudgeClient
from auto_eval.task_control import PauseRequested, bind_pause_check, check_pause, pause_aware
from test_request_transport import (
    LoopbackProvider, ObservedThrottle, complete, sdk_client, send_json, send_sse,
)


@pytest.fixture(autouse=True)
def local_proxy_bypass(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.mark.asyncio
async def test_completed_permit_is_returned_before_pause_and_context_is_restored():
    paused = False
    lock = asyncio.Lock()

    async def acquire():
        nonlocal paused
        await lock.acquire()
        paused = True
        return True

    with bind_pause_check(lambda: paused):
        assert await pause_aware(acquire(), on_cancel=lambda _: lock.release())
        try:
            with pytest.raises(PauseRequested):
                check_pause()
        finally:
            lock.release()
    check_pause()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_external_cancel_simultaneous_with_acquisition_releases_permit():
    lock = asyncio.Lock()

    async def waiting():
        owner = asyncio.current_task()

        async def acquire():
            await lock.acquire()
            owner.cancel()
            return True

        with bind_pause_check(lambda: False):
            await pause_aware(acquire(), on_cancel=lambda _: lock.release())

    waiter = asyncio.create_task(waiting())
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not lock.locked()


@pytest.mark.asyncio
async def test_pause_cancels_inflight_slot_wait_without_reservation_or_slot_leak():
    paused = False
    throttle = ObservedThrottle(max_inflight=1)
    first = await throttle.acquire(64)
    with bind_pause_check(lambda: paused):
        waiting = asyncio.create_task(throttle.acquire(64))
    try:
        async def wait_for_slot_queue():
            while not throttle._wait_reasons.get("inflight"):
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait_for_slot_queue(), 1)
        paused = True
        with pytest.raises(PauseRequested):
            await asyncio.wait_for(waiting, 1)
        assert throttle.snapshot()["pending"] == 0
        assert throttle.snapshot()["inflight"] == 1
        assert throttle.snapshot()["total_requests"] == 1
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        throttle.finish(first, {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10})
    assert throttle._slots._value == 1


@pytest.mark.asyncio
async def test_pause_drains_sent_stream_and_removes_dispatch_and_quota_waiters():
    paused = False
    finish = asyncio.Event()

    async def respond(request, writer):
        await send_sse(writer, finish if request.index == 0 else None)

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        throttle = ObservedThrottle(max_inflight=1)
        jobs = []
        try:
            with bind_pause_check(lambda: paused):
                first = asyncio.create_task(complete(client, throttle))
                jobs.append(first)
                await provider.wait_requests(1)
                waiters = [asyncio.create_task(complete(client, throttle)) for _ in range(4)]
                jobs.extend(waiters)
                # The first waiter waits for in-flight capacity and holds the
                # dispatch turn; the others queue for that same turn.
                async def wait_backlog():
                    while throttle.snapshot()["pending"] < 4:
                        await asyncio.sleep(.005)
                await asyncio.wait_for(wait_backlog(), 3)
                paused = True
                outcomes = await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 1)
                assert all(isinstance(result, asyncio.CancelledError) for result in outcomes)
                assert len(provider.requests) == 1
                assert not first.done()
                assert not throttle.dispatch_lock.locked()
                finish.set()
                response = await asyncio.wait_for(first, 3)
                assert response.usage.total_tokens == 10
            assert throttle.snapshot()["pending"] == 0
            assert throttle.snapshot()["inflight"] == 0
            assert throttle.snapshot()["total_requests"] == 1
            # A different task must not inherit the previous task's pause.
            assert (await complete(client, throttle)).choices[0].message.content == "ok"
            assert len(provider.requests) == 2
        finally:
            finish.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("paced", [False, True])
async def test_pause_during_pool_wait_blocks_headers_for_paced_and_other_models(paced):
    paused = False
    release = asyncio.Event()
    prepared = asyncio.Event()

    async def respond(request, writer):
        if request.path.endswith("/blocker"):
            await release.wait()
            await send_json(writer)
        else:
            await send_sse(writer)

    async def observe_prepared(request):
        if request.method == "POST":
            prepared.set()

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider, pool_size=1)
        client._client.event_hooks["request"].append(observe_prepared)
        throttle = ObservedThrottle() if paced else None
        blocker = asyncio.create_task(client._client.get(provider.url + "/blocker"))
        job = None
        try:
            await provider.wait_requests(1)
            with bind_pause_check(lambda: paused):
                job = asyncio.create_task(complete(client, throttle))
            await asyncio.wait_for(prepared.wait(), 3)
            paused = True
            release.set()
            await blocker
            with pytest.raises(PauseRequested):
                await asyncio.wait_for(job, 3)
            assert len(provider.requests) == 1
            if throttle is not None:
                assert not throttle.admissions
                assert not throttle.dispatch_lock.locked()
                assert throttle.snapshot()["pending"] == throttle.snapshot()["inflight"] == 0
        finally:
            release.set()
            for running in (blocker, job):
                if running is not None:
                    running.cancel()
            await asyncio.gather(*[running for running in (blocker, job) if running is not None], return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,message", [
    (400, "stream_options include_usage is unsupported"),
    (429, "limit_requests"),
])
async def test_pause_prevents_usage_fallback_and_error_retries(status, message):
    paused = False

    async def respond(request, writer):
        nonlocal paused
        paused = True
        await send_json(writer, status, {"error": {"message": message, "type": "test_error"}})

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        throttle = ObservedThrottle()
        try:
            with bind_pause_check(lambda: paused):
                with pytest.raises(PauseRequested):
                    await asyncio.wait_for(complete(client, throttle, max_attempts=3), 3)
            assert len(provider.requests) == len(throttle.admissions) == 1
            assert not throttle.dispatch_lock.locked()
            assert throttle.snapshot()["inflight"] == 0
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pause_interrupts_retry_backoff_without_another_request(monkeypatch):
    paused = False
    backoff = asyncio.Event()

    def long_backoff(*_):
        backoff.set()
        return 20

    monkeypatch.setattr("auto_eval.llm_stream.random.uniform", long_backoff)

    async def respond(request, writer):
        await send_json(writer, 503, {"error": {"message": "temporarily unavailable", "type": "test_error"}})

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        job = None
        try:
            with bind_pause_check(lambda: paused):
                job = asyncio.create_task(complete(client, None, max_attempts=3))
            await asyncio.wait_for(backoff.wait(), 3)
            paused = True
            with pytest.raises(PauseRequested):
                await asyncio.wait_for(job, 1)
            assert len(provider.requests) == 1
        finally:
            if job is not None:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_paused_json_repair_never_enters_the_network():
    async def respond(request, writer):
        await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = JudgeClient(JudgeConfig(name="judge_2", model="qwen3.8-flash", base_url=provider.url))
        try:
            with bind_pause_check(lambda: True):
                with pytest.raises(PauseRequested):
                    await client.repair_json('{"score":')
            assert not provider.requests
        finally:
            await client.aclose()
