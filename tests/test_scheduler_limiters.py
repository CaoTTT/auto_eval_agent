"""Direct coverage for the global model and pipeline limiters."""
import asyncio

import pytest

from auto_eval.web.scheduler import (
    DEFAULT_CONCURRENCY,
    DEFAULT_MAX_IN_FLIGHT,
    ResizableLimiter,
    TokenBucketRateLimiter,
)


def test_global_pacing_defaults():
    assert DEFAULT_CONCURRENCY == 5
    assert DEFAULT_MAX_IN_FLIGHT == 40


@pytest.mark.asyncio
async def test_token_limiter_bounds_inflight_and_releases():
    limiter = TokenBucketRateLimiter(rate=1000, max_in_flight=2)
    await asyncio.wait_for(limiter.acquire(), timeout=1)
    await asyncio.wait_for(limiter.acquire(), timeout=1)
    pending = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    assert not pending.done()
    stats = limiter.stats()
    assert stats["running"] == 2
    assert stats["queued"] == 1

    limiter.release()
    limiter.release()
    await asyncio.wait_for(pending, timeout=1)
    limiter.release()

    assert limiter.stats()["running"] == 0


@pytest.mark.asyncio
async def test_cancelled_token_waiter_releases_no_slot():
    limiter = TokenBucketRateLimiter(rate=1000, max_in_flight=1)
    await asyncio.wait_for(limiter.acquire(), timeout=1)
    pending = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    stats = limiter.stats()
    assert stats["running"] == 1
    assert stats["queued"] == 0
    limiter.release()
    assert limiter.stats()["running"] == 0


@pytest.mark.asyncio
async def test_growing_token_inflight_limit_wakes_waiters():
    limiter = TokenBucketRateLimiter(rate=1000, max_in_flight=1)
    await asyncio.wait_for(limiter.acquire(), timeout=1)
    pending = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    limiter.set_max_in_flight(2)
    await asyncio.wait_for(pending, timeout=1)
    limiter.release()
    limiter.release()

    assert limiter.stats()["running"] == 0


@pytest.mark.asyncio
async def test_resizable_limiter_grows_fifo_and_releases():
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    first = asyncio.create_task(limiter.acquire())
    second = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    limiter.set_limit(3)
    await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert limiter.stats()["running"] == 3
    for _ in range(3):
        limiter.release()
    assert limiter.stats()["running"] == 0


@pytest.mark.asyncio
async def test_cancelled_resizable_waiter_does_not_leak_slot():
    limiter = ResizableLimiter(1)
    await limiter.acquire()
    pending = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert limiter.stats()["running"] == 1
    assert limiter.stats()["queued"] == 0
    limiter.release()
    successor = asyncio.create_task(limiter.acquire())
    await asyncio.wait_for(successor, timeout=1)
    limiter.release()
    assert limiter.stats()["running"] == 0
