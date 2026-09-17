"""Pause stops new model dispatch while preserving completed, billable work."""
import asyncio
import copy
import threading
from pathlib import Path

import pytest

from auto_eval.config import load_config
from auto_eval import preparation
from auto_eval.observability import current_context
from auto_eval.task_control import PauseRequested, bind_pause_check, check_pause
from auto_eval.web import history, runner, tasks
from auto_eval.web.execution_control import resume_indexes
from auto_eval.web.tasks import Task


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(tasks, "TASKS", tasks.OrderedDict())
    monkeypatch.setattr(runner, "retire_task", lambda _: None)

    class Client:
        def __init__(self, cfg):
            self.config = cfg

        async def aclose(self):
            pass

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "RichContentJudge", lambda *args: None)
    monkeypatch.setattr(runner, "VisualCompareJudge", lambda *args: None)
    monkeypatch.setattr(runner, "supports_bailian_pacing", lambda _: False)
    return load_config(Path(__file__).parents[1] / "config")


def make_task(count, concurrency=128):
    return Task(
        id="pause_dispatch", mode="compare", active_runs=1,
        options={"concurrency": concurrency},
        items=[{"id": f"q{index}", "query": str(index)} for index in range(count)],
    )


def prepare(item):
    return {**item, "frames1": ["prepared-a"], "frames2": ["prepared-b"]}


def assert_paused_without_results(task, count):
    assert task.status == "paused"
    assert task.active_runs == 0
    assert task.results == []
    assert task.done_total == 0
    assert task.error is None
    assert runner._summarize(task)["failed"] == 0
    assert resume_indexes(task) == list(range(count))
    assert all(progress["status"] == "paused" for progress in task.item_progress.values())
    snapshot = history.load_snapshot(task.id)
    assert snapshot["status"] == "paused"
    assert snapshot["results"] == []
    return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation_stage", ["visual_evidence", "query_images"])
async def test_pause_after_concurrent_media_prepare_prevents_model_dispatch(config, monkeypatch, preparation_stage):
    count = 24
    task = make_task(count)
    all_preparing, release = asyncio.Event(), asyncio.Event()
    preparing, calls = [], []
    if preparation_stage == "query_images":
        for item in task.items:
            item.update(prepare(item), query_images=["query-image"])

    async def blocked_prepare(fn, item, **kwargs):
        preparing.append(int(item["query"]))
        if len(preparing) == count:
            all_preparing.set()
        await release.wait()
        return prepare(item)

    async def evaluate(mode, index, item, **kwargs):
        calls.append(index)
        return {"item_id": item["id"], "query": item["query"]}

    monkeypatch.setattr(runner, "run_preparation", blocked_prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    job = asyncio.create_task(runner.run_eval(task, config))
    try:
        await asyncio.wait_for(all_preparing.wait(), 5)
        task.execution_control["state"] = "pausing"
    finally:
        release.set()
        await asyncio.wait_for(job, 5)

    assert calls == [], "Prepared cases must not start model calls after pause"
    snapshot = assert_paused_without_results(task, count)

    # Pausing must leave every unsubmitted case recoverable from durable state.
    restored = tasks._task_from_snapshot(snapshot, task.id)
    indexes = resume_indexes(restored)
    restored.active_runs = 1
    restored.execution_control.update(state="queued", pending_indexes=indexes, concurrency=3)
    restored.execution_control["resumes"] = [{
        "id": "resume_dispatch", "status": "queued", "indexes": indexes,
        "concurrency": 3, "completed": 0, "failed": 0,
    }]
    await asyncio.wait_for(runner.run_resume(restored, config), 5)
    assert restored.status == "done"
    assert sorted(calls) == list(range(count))
    assert len(restored.results) == count
    assert resume_indexes(restored) == []


@pytest.mark.asyncio
async def test_pause_keeps_inflight_result_but_drops_prepared_and_queued_dispatch(config, monkeypatch):
    task = make_task(5, concurrency=2)
    task.items[0].update(prepare(task.items[0]))
    model_started, prep_started = asyncio.Event(), asyncio.Event()
    release_model, release_prep = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked_prepare(fn, item, **kwargs):
        prep_started.set()
        await release_prep.wait()
        return prepare(item)

    async def evaluate(mode, index, item, **kwargs):
        calls.append(index)
        if index == 0:
            model_started.set()
            await release_model.wait()
        return {"item_id": item["id"], "query": item["query"], "answer": "completed"}

    monkeypatch.setattr(runner, "run_preparation", blocked_prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    job = asyncio.create_task(runner.run_eval(task, config))
    try:
        await asyncio.wait_for(asyncio.gather(model_started.wait(), prep_started.wait()), 5)
        task.execution_control["state"] = "pausing"
    finally:
        release_prep.set()
        release_model.set()
        await asyncio.wait_for(job, 5)

    assert calls == [0], "Pause may drain an existing call but cannot dispatch prepared cases"
    assert task.status == "paused"
    assert task.done_total == 1
    assert task.error is None
    assert resume_indexes(task) == [1, 2, 3, 4]
    assert task.item_progress["0"]["status"] == "done"
    assert all(task.item_progress[str(index)]["status"] == "paused" for index in range(1, 5))
    saved = history.load_snapshot(task.id)["results"]
    assert len(saved) == 1 and saved[0]["index"] == 0
    assert saved[0]["answer"] == "completed"


@pytest.mark.asyncio
async def test_pause_during_outer_retry_wait_prevents_second_evaluation(config, monkeypatch):
    task = make_task(1)
    task.items[0].update(prepare(task.items[0]))
    retry_wait, release = asyncio.Event(), asyncio.Event()
    calls = []

    class AsyncioWithRetryBarrier:
        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            if delay == 1.0:
                retry_wait.set()
                await release.wait()
            else:
                await asyncio.sleep(delay)

    async def evaluate(mode, index, item, **kwargs):
        calls.append(index)
        if len(calls) == 1:
            raise ConnectionError("provider connection reset")
        return {"item_id": item["id"]}

    monkeypatch.setattr(runner, "asyncio", AsyncioWithRetryBarrier())
    monkeypatch.setattr(runner, "is_retriable_llm_error", lambda exc: isinstance(exc, ConnectionError))
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    job = asyncio.create_task(runner.run_eval(task, config))
    try:
        await asyncio.wait_for(retry_wait.wait(), 5)
        task.execution_control["state"] = "pausing"
    finally:
        release.set()
        await asyncio.wait_for(job, 5)

    assert calls == [0], "An outer retry is a new model call and must honor pause"
    assert_paused_without_results(task, 1)


@pytest.mark.asyncio
async def test_pause_removes_media_waiter_without_starting_worker_or_leaking_permit():
    queued = asyncio.Event()
    state, calls = {"paused": False}, []

    class ObservedLimiter(preparation.PreparationLimiter):
        async def acquire(self, *, priority=0):
            if not self._available:
                queued.set()
            await super().acquire(priority=priority)

    limit = ObservedLimiter(1)
    await limit.acquire()
    with bind_pause_check(lambda: state["paused"]), preparation.preparation_limit(limit):
        job = asyncio.create_task(preparation.run_preparation(lambda: calls.append("unexpected"), timeout=5))
        try:
            await asyncio.wait_for(queued.wait(), 5)
            state["paused"] = True
            with pytest.raises(PauseRequested):
                await asyncio.wait_for(job, 5)
            assert calls == []
            assert limit._available == 0, "The external holder still owns the only permit"
        finally:
            limit.release()

        assert limit._available == 1
        assert not limit._waiters
        state["paused"] = False
        result = await asyncio.wait_for(preparation.run_preparation(lambda: "resumed", timeout=5), 5)
        assert result == "resumed"
        assert limit._available == 1


@pytest.mark.asyncio
async def test_task_pause_waits_for_owned_media_thread_and_returns_all_permits(config, monkeypatch):
    task = make_task(2, concurrency=2)
    started, queued, cancelled, exited = (asyncio.Event() for _ in range(4))
    release_exit = threading.Event()
    worker_calls, model_calls = [], []
    loop = asyncio.get_running_loop()

    class ObservedLimiter(preparation.PreparationLimiter):
        async def acquire(self, *, priority=0):
            if not self._available:
                queued.set()
            await super().acquire(priority=priority)

    limit = ObservedLimiter(1)

    def cooperative_worker(item, **kwargs):
        worker_calls.append(kwargs["item_index"])
        loop.call_soon_threadsafe(started.set)
        try:
            while True:
                try:
                    preparation.check_preparation()
                except preparation.PreparationStopped:
                    loop.call_soon_threadsafe(cancelled.set)
                    assert release_exit.wait(10), "Test must release the cancelled worker"
                    raise
                if release_exit.wait(.01):
                    return prepare(item)
        finally:
            loop.call_soon_threadsafe(exited.set)

    async def evaluate(mode, index, item, **kwargs):
        model_calls.append(index)
        return {"item_id": item["id"]}

    monkeypatch.setattr(runner, "PreparationLimiter", lambda capacity: limit)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", cooperative_worker)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    job = asyncio.create_task(runner.run_eval(task, config))
    try:
        await asyncio.wait_for(asyncio.gather(started.wait(), queued.wait()), 5)
        task.execution_control["state"] = "pausing"
        await asyncio.wait_for(cancelled.wait(), 5)
        assert not exited.is_set()
        assert not job.done(), "A paused terminal cannot outlive an owned media thread"
        assert task.active_runs == 1
        assert task.status != "paused"
        assert worker_calls == [0], "The queued worker must never start after pause"
        assert model_calls == []
    finally:
        release_exit.set()
        await asyncio.wait_for(job, 5)

    assert exited.is_set()
    assert worker_calls == [0]
    assert_paused_without_results(task, 2)
    assert limit._available == 1
    assert not limit._waiters

    # Reuse the exact same limiter so a leaked permit would block new work.
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", lambda item, **kwargs: prepare(item))
    next_task = make_task(2, concurrency=2)
    next_task.id = "pause_dispatch_after_cleanup"
    await asyncio.wait_for(runner.run_eval(next_task, config), 5)
    assert next_task.status == "done"
    assert sorted(model_calls) == [0, 1]
    assert limit._available == 1


@pytest.mark.asyncio
async def test_pause_before_repair_preserves_completed_call_trace_without_result(config, monkeypatch, tmp_path):
    task = make_task(1)
    task.items[0].update(prepare(task.items[0]))
    trace_path = str(tmp_path / "judge_calls.jsonl")
    call_record = {
        "task_id": task.id, "item_index": 0, "status": "success",
        "model": "qwen3.8-flash", "rounds": 1,
        "llm_rounds": [{
            "round": 1, "content": "{incomplete-json", "finish_reason": "stop",
            "usage": {"prompt_tokens": 123, "completion_tokens": 45, "reasoning_tokens": 10},
        }],
    }
    flushed = []

    async def evaluate(mode, index, item, **kwargs):
        # JudgeClient._write_trace uses this callback to defer an already
        # completed call's audit record until the item settles in the runner.
        callback = current_context().judge_trace_callback
        assert callback is not None
        callback(trace_path, copy.deepcopy(call_record))
        task.execution_control["state"] = "pausing"
        check_pause()  # The next JSON-repair dispatch is stopped cooperatively.
        pytest.fail("A paused repair must not reach the provider")

    def flush(records, result):
        flushed.append((copy.deepcopy(records), copy.deepcopy(result)))
        return len(records)

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "flush_web_trace_records", flush)
    await asyncio.wait_for(runner.run_eval(task, config), 5)

    assert len(flushed) == 1
    records, pending = flushed[0]
    assert records == [(trace_path, call_record)]
    assert pending["index"] == 0 and pending["item_id"] == "q0"
    assert pending["evaluation_status"] == "paused"
    assert pending["evaluation_pending"] is True
    assert "error" not in pending
    assert_paused_without_results(task, 1)
