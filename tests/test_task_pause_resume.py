"""Cooperative stop, durable checkpoints and resume selection (no model calls)."""
import asyncio
import copy
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from auto_eval.config import load_config
from auto_eval.web import history, persistence, runner, scheduler, server, tasks
from auto_eval.web.execution_control import resume_indexes
from auto_eval.web.tasks import Task, latest_results_by_index


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(tasks, "TASKS", tasks.OrderedDict())
    monkeypatch.setattr(runner, "retire_task", lambda _: None)
    return tmp_path


def make_task(count=5, mode="compare"):
    return Task(id="pause_test", mode=mode, items=[
        {"id": f"q{i}", "query": str(i), "frames1": ["a"], "frames2": ["b"], "frames": ["a"]}
        for i in range(count)
    ], options={"concurrency": 2}, active_runs=1)


def fake_clients(monkeypatch):
    class Client:
        def __init__(self, cfg):
            self.config = cfg

        async def aclose(self):
            pass

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "RichContentJudge", lambda *a: None)
    monkeypatch.setattr(runner, "VisualCompareJudge", lambda *a: None)
    monkeypatch.setattr(runner, "supports_bailian_pacing", lambda _: False)
    return load_config(Path(__file__).parents[1] / "config")


def queue_resume(task, indexes, concurrency=3):
    task.active_runs = 1
    task.execution_control.update(state="queued", pending_indexes=indexes.copy(), concurrency=concurrency)
    task.execution_control.setdefault("resumes", []).append({
        "id": "resume_test", "status": "queued", "indexes": indexes,
        "concurrency": concurrency, "completed": 0, "failed": 0,
    })


@pytest.mark.asyncio
async def test_pause_drains_inflight_then_resume_uses_new_semaphore(storage, monkeypatch):
    task = make_task(7)
    cfg = fake_clients(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()
    calls, active, peak = [], 0, 0

    async def evaluate(mode, index, item, **kwargs):
        nonlocal active, peak
        calls.append(index)
        active += 1
        peak = max(peak, active)
        if len(calls) == 2:
            started.set()
        await release.wait()
        await asyncio.sleep(.01)
        active -= 1
        return {"item_id": item["id"], "query": item["query"]}

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    events = task.subscribe()
    job = asyncio.create_task(runner.run_eval(task, cfg))
    await asyncio.wait_for(started.wait(), 3)
    task.execution_control.update(state="pausing")
    await asyncio.sleep(.02)
    assert not job.done() and len(calls) == 2
    release.set()
    await asyncio.wait_for(job, 3)
    assert task.status == "paused"
    assert task.active_runs == 0
    assert sorted(calls) == [0, 1]
    stored = history.load_snapshot(task.id)
    assert len(stored["results"]) == 2
    messages = []
    while not events.empty():
        messages.append(events.get_nowait()["event"])
    assert messages[-1] == "paused" and "done" not in messages

    restored = tasks._task_from_snapshot(stored, task.id)
    preserved = copy.deepcopy(restored.results)
    queue_resume(restored, resume_indexes(restored), concurrency=3)
    peak = 0
    await runner.run_resume(restored, cfg)
    assert restored.status == "done"
    assert peak == 3
    assert sorted(calls) == list(range(7))
    assert restored.results[:2] == preserved
    assert restored.options["concurrency"] == 2
    assert restored.execution_control["resumes"][-1]["completed"] == 5
    assert not restored.execution_control["pending_indexes"]
    assert len(history.load_snapshot(task.id)["results"]) == 7
    assert all(restored.item_progress[str(i)]["status"] == "done" for i in range(2, 7))


@pytest.mark.asyncio
async def test_resume_sessions_rebuild_context_without_repeating_prefix(storage, monkeypatch):
    task = make_task(4, "rich_content")
    for i, item in enumerate(task.items):
        item.update(session_group="s", turn_index=i)
    task.items[1]["context"] = "历史对话总结：\nOLD"
    task.results = [{"index": 0, "turn_summary": "saved-first"}, {"index": 1, "error": "failed"}]
    assert resume_indexes(task) == [2, 3]
    assert resume_indexes(task, True) == [1, 2, 3]
    contexts = []

    async def evaluate(mode, index, item, **kwargs):
        contexts.append((index, item.get("context", "")))
        return {"turn_summary": f"summary-{index}"}

    cfg = fake_clients(monkeypatch)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    queue_resume(task, resume_indexes(task, True))
    await runner.run_resume(task, cfg)
    assert [i for i, _ in contexts] == [1, 2, 3]
    assert "saved-first" in contexts[0][1]
    assert all("OLD" not in context and context.count("历史对话总结") == 1 for _, context in contexts)
    assert "summary-1" in contexts[2][1] and "summary-2" in contexts[2][1]


@pytest.mark.asyncio
async def test_resume_preserves_new_failures_and_can_pause_again(storage, monkeypatch):
    task = make_task(4)
    cfg = fake_clients(monkeypatch)
    task.options["concurrency"] = 1
    queue_resume(task, [0, 1, 2, 3], concurrency=1)

    async def evaluate(mode, index, item, **kwargs):
        task.execution_control["state"] = "pausing"
        return {"error": "provider failure"}

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    await runner.run_resume(task, cfg)
    assert task.status == "paused"
    assert latest_results_by_index(task)[0]["error"] == "provider failure"
    assert task.execution_control["pending_indexes"] == [1, 2, 3]
    assert task.execution_control["resumes"][-1]["status"] == "paused"
    assert task.item_progress["0"]["status"] == "error"
    assert task.item_progress["1"]["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_queued_job_does_not_cancel_running_neighbor(storage, monkeypatch):
    queue = scheduler.EvalScheduler()
    monkeypatch.setattr(server, "EVAL_SCHEDULER", queue)
    running, waiting = make_task(1), make_task(1)
    running.id, waiting.id = "first", "second"
    running.active_runs = waiting.active_runs = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def block(task, cfg):
        started.set()
        await release.wait()
        task.active_runs -= 1
        task.status = "done"

    async def get(_):
        return waiting

    monkeypatch.setattr(server, "get_task_async", get)
    queue.enqueue(running, None, block)
    await started.wait()
    queue.enqueue(waiting, None, block)
    try:
        response = await server.api_pause(waiting.id)
        assert response["status"] == "paused"
        assert queue.snapshot()["queued"] == []
        assert queue.snapshot()["running"]["task_id"] == "first"
        assert waiting.active_runs == 0
        assert history.load_snapshot(waiting.id)["status"] == "paused"
    finally:
        release.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_resume_endpoint_idempotency_and_no_input_mutation(storage, monkeypatch):
    task = make_task(3)
    task.judge_runtime = {"version": 1, "profile_id": "test", "judges": [
        {"name": "judge", "model": "fake", "enable_thinking": False},
    ]}
    task.status, task.active_runs = "paused", 0
    task.results = [{"index": 0}, {"index": 1, "error": "failed"}]
    original = copy.deepcopy(task.items)

    async def get(_):
        return task

    class Queue:
        def enqueue(self, parent, cfg, fn):
            parent.active_runs += 1
            parent.status = "queued"
            return 2

    monkeypatch.setattr(server, "get_task_async", get)
    monkeypatch.setattr(server, "cfg", lambda: None)
    monkeypatch.setattr(server, "EVAL_SCHEDULER", Queue())
    req = server.ResumeReq(concurrency=8, idempotency_key="once")
    response = await server.api_resume(task.id, req)
    assert response["selected"] == 1
    assert task.execution_control["resumes"][-1]["indexes"] == [2]
    assert (await server.api_resume(task.id, req))["idempotent_replay"]
    with pytest.raises(HTTPException) as exc:
        await server.api_resume(task.id, server.ResumeReq(concurrency=4))
    assert exc.value.status_code == 409
    assert task.items == original and task.options == {"concurrency": 2}


@pytest.mark.parametrize("concurrency", [0, 129, True, 2.5, "4"])
def test_resume_concurrency_is_strict(concurrency):
    with pytest.raises(ValidationError):
        server.ResumeReq(concurrency=concurrency)


def test_crash_preserves_resume_selection_and_attempt_audit():
    task = make_task(4)
    task.status = "running"
    task.results = [{"index": 0}, {"index": 1}]
    queue_resume(task, [2, 3])
    restored = tasks._task_from_snapshot(history.task_to_snapshot(task), task.id)
    assert restored.status == "error"
    assert restored.execution_control["state"] == "interrupted"
    assert restored.execution_control["resumes"][-1]["status"] == "error"
    assert resume_indexes(restored) == [2, 3]


@pytest.mark.asyncio
async def test_pause_save_failure_retains_task_for_recovery(storage, monkeypatch):
    task = make_task(1)
    task.active_runs = 0
    task.execution_control["state"] = "pausing"
    tasks.TASKS[task.id] = task
    monkeypatch.setattr(runner, "save_task", lambda _: False)
    await runner.finish_pause(task)
    tasks.retire_task(task)
    assert tasks.TASKS[task.id] is task
    assert task.execution_control["save_error"]
    monkeypatch.setattr(runner, "save_task", history.save_task)
    task.execution_control["state"] = "pausing"
    await runner.finish_pause(task)
    assert not task.execution_control.get("save_error")
    assert not history.load_snapshot(task.id)["execution_control"].get("save_error")


@pytest.mark.asyncio
async def test_update_batch_resume_replaces_old_success_and_restores_context(storage, monkeypatch):
    task = make_task(3, "rich_content")
    task.status = "done"
    task.results = [{"index": i, "turn_summary": "OLD"} for i in range(3)]
    task.execution_control["update_batches"] = {"batch": {
        "remaining": [0, 1, 2], "options": {"concurrency": 1}, "prior_summary": "",
    }}
    contexts = []
    cfg = fake_clients(monkeypatch)

    async def evaluate(mode, index, item, **kwargs):
        contexts.append((index, item.get("context", "")))
        if index == 0:
            task.execution_control["state"] = "pausing"
        return {"turn_summary": f"new-{index}"}

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    await runner.run_update_batch(task, cfg, list(enumerate(task.items)), options=task.options, batch_id="batch")
    assert task.status == "paused"
    assert task.execution_control["update_batches"]["batch"]["remaining"] == [1, 2]
    restored = tasks._task_from_snapshot(history.load_snapshot(task.id), task.id)
    queue_resume(restored, resume_indexes(restored))
    await runner.run_resume(restored, cfg)
    assert restored.status == "done"
    assert [i for i, _ in contexts] == [0, 1, 2]
    assert "new-0" in contexts[1][1] and "OLD" not in contexts[1][1]
    assert "new-1" in contexts[2][1]
    assert latest_results_by_index(restored)[1]["turn_summary"] == "new-1"
    assert not restored.execution_control["update_batches"]
    assert not restored.execution_control["pending_indexes"]


@pytest.mark.asyncio
async def test_paused_sse_replay_is_terminal(storage, monkeypatch):
    task = make_task(1)
    task.status, task.active_runs = "paused", 0
    task.execution_control["state"] = "paused"
    monkeypatch.setattr(server, "peek_task", lambda _: task)
    response = await server.api_stream(task.id, compact=True)
    output = [chunk async for chunk in response.body_iterator]
    assert "event: paused" in output[-1]
    assert not task.subscribers


def test_pause_duration_excluded_across_multiple_resumes(monkeypatch):
    clock = {"wall": 1000, "mono": 10}
    monkeypatch.setattr(tasks, "time", SimpleNamespace(time=lambda: clock["wall"], monotonic=lambda: clock["mono"]))
    task = make_task(1)
    task.start_timing()
    clock.update(wall=1010, mono=20)
    task.finish_timing()
    clock.update(wall=9000, mono=8010)
    task.resume_timing()
    clock.update(wall=9020, mono=8030)
    task.finish_timing()
    assert task.timing_snapshot()["elapsed_s"] == 30
    assert task.timing_snapshot()["started_at"] == 1000


def test_rich_content_summary_separates_unfinished_from_failed():
    task = make_task(4, "rich_content")
    task.results = [{"index": 0}, {"index": 1, "error": "failure"}]
    summary = runner._summarize(task)
    assert (summary["done"], summary["failed"], summary["unfinished"]) == (1, 1, 2)


@pytest.mark.asyncio
async def test_paused_retry_retains_unattempted_dependency_indexes(storage, monkeypatch):
    task = make_task(3, "rich_content")
    task.status = "done"
    task.results = [{"index": 0, "error": "old"}, {"index": 1}, {"index": 2}]
    for index, item in enumerate(task.items):
        item.update(session_group="group", turn_index=index)
    task.execution_control["pending_indexes"] = [0, 1, 2]
    task.retry_runs["retry"] = {"indexes": [0, 1, 2], "items": {}, "options": {},
                                 "reasons": {"1": "session_dependency", "2": "session_dependency"}}
    cfg = fake_clients(monkeypatch)

    async def evaluate(mode, index, item, **kwargs):
        task.execution_control["state"] = "pausing"
        return {"turn_summary": "fixed"}

    monkeypatch.setattr(runner, "_eval_one", evaluate)
    await runner.run_retry(task, cfg, "retry")
    assert task.status == "paused"
    assert task.retry_runs["retry"]["status"] == "paused"
    assert resume_indexes(task) == [1, 2]
    assert latest_results_by_index(task)[0]["turn_summary"] == "fixed"


def test_pause_resume_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend regression harness")
    result = subprocess.run([node, str(Path(__file__).with_name("test_task_pause_resume_ui.cjs"))],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
