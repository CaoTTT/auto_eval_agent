"""Priority admission must not leak permits or detach cancelled media workers."""
import asyncio
import threading

import pytest

from auto_eval.preparation import (
    PreparationLimiter, check_preparation, preparation_limit, run_preparation,
)


async def assert_one_reusable_permit(limiter):
    """Prove a capacity-one limiter is neither exhausted nor over-released."""
    await asyncio.wait_for(limiter.acquire(), timeout=1)
    blocked = asyncio.create_task(limiter.acquire())
    try:
        await asyncio.sleep(0)
        assert not blocked.done()
        limiter.release()
        await asyncio.wait_for(blocked, timeout=1)
    finally:
        if not blocked.done():
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
        limiter.release()


@pytest.mark.parametrize("capacity", [0, -1])
def test_preparation_capacity_must_be_positive(capacity):
    with pytest.raises(ValueError, match="positive"):
        PreparationLimiter(capacity)


@pytest.mark.asyncio
async def test_ready_case_encoding_precedes_later_case_extraction():
    limiter = PreparationLimiter(1)
    order = []

    async def case(index):
        for stage in ("extract", "encode"):
            await limiter.acquire(priority=index)
            try:
                order.append((index, stage))
                await asyncio.sleep(0)
            finally:
                limiter.release()

    await asyncio.wait_for(asyncio.gather(*(case(i) for i in range(6))), timeout=1)

    # A permit already handed to case 1 is not preempted, but ready case 0
    # must advance before the rest of the extraction queue is admitted.
    assert order.index((0, "encode")) < order.index((2, "extract"))
    assert order.index((1, "encode")) < order.index((2, "extract"))
    assert len(order) == 12
    await assert_one_reusable_permit(limiter)


@pytest.mark.asyncio
async def test_equal_priority_waiters_keep_arrival_order():
    limiter = PreparationLimiter(1)
    await limiter.acquire()
    order = []

    async def stage(index):
        await limiter.acquire(priority=5)
        try:
            order.append(index)
        finally:
            limiter.release()

    queued = [asyncio.create_task(stage(i)) for i in range(6)]
    await asyncio.sleep(0)
    limiter.release()
    await asyncio.wait_for(asyncio.gather(*queued), timeout=1)

    assert order == list(range(6))
    await assert_one_reusable_permit(limiter)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_handoff", [False, True])
async def test_cancelled_waiter_returns_exactly_one_permit(after_handoff):
    limiter = PreparationLimiter(1)
    await limiter.acquire()
    victim = asyncio.create_task(limiter.acquire(priority=0))
    survivor = asyncio.create_task(limiter.acquire(priority=1))
    await asyncio.sleep(0)

    if after_handoff:
        # Cancel after set_result but before the recipient resumes acquire().
        limiter.release()
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim
    if not after_handoff:
        limiter.release()

    await asyncio.wait_for(survivor, timeout=1)
    limiter.release()
    await assert_one_reusable_permit(limiter)


@pytest.mark.asyncio
async def test_cancelled_preparation_waiter_never_starts_worker():
    limiter = PreparationLimiter(1)
    await limiter.acquire()
    called = threading.Event()
    with preparation_limit(limiter, priority=0):
        queued = asyncio.create_task(run_preparation(called.set, timeout=1))
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    limiter.release()

    await assert_one_reusable_permit(limiter)
    assert not called.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancel", "timeout"])
async def test_stopped_worker_keeps_permit_until_cooperative_cleanup(outcome):
    limiter = PreparationLimiter(1)
    started, unblock, cleaned, successor_started = (threading.Event() for _ in range(4))

    def work():
        started.set()
        try:
            assert unblock.wait(3), "test did not release the atomic media operation"
            check_preparation()
            pytest.fail("stopped preparation continued beyond its checkpoint")
        finally:
            cleaned.set()

    def next_work():
        assert cleaned.is_set(), "a replacement worker started before cleanup"
        successor_started.set()
        return "ready"

    async def wait_until_started():
        while not started.is_set():
            await asyncio.sleep(.001)

    with preparation_limit(limiter, priority=0):
        job = asyncio.create_task(run_preparation(work, timeout=.05 if outcome == "timeout" else 3))
    try:
        await asyncio.wait_for(wait_until_started(), timeout=1)
        with preparation_limit(limiter, priority=1):
            successor = asyncio.create_task(run_preparation(next_work, timeout=1))
        if outcome == "cancel":
            job.cancel()
            await asyncio.sleep(0)
            job.cancel()  # Repeated shutdown cancellation must retain ownership.
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(.1)
        assert not job.done()
        assert not cleaned.is_set()
        assert not successor_started.is_set()
    finally:
        unblock.set()

    with pytest.raises(asyncio.CancelledError if outcome == "cancel" else TimeoutError):
        await asyncio.wait_for(job, timeout=1)
    assert await asyncio.wait_for(successor, timeout=1) == "ready"
    assert cleaned.is_set()
    await assert_one_reusable_permit(limiter)
