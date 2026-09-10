"""全量批跑 FIFO 队列与前端入口的回归测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from auto_eval.web import scheduler as scheduler_module
from auto_eval.web.history import _apply_interrupted_status
from auto_eval.web.scheduler import EvalScheduler
from auto_eval.web.tasks import Task


def _task(task_id: str, concurrency: int) -> Task:
    return Task(
        id=task_id,
        mode="rich_content",
        items=[{"id": f"{task_id}-1", "query": task_id}],
        options={"concurrency": concurrency},
        dataset_name=f"dataset-{task_id}",
    )


@pytest.mark.asyncio
async def test_scheduler_runs_tasks_fifo_without_overlap(monkeypatch):
    monkeypatch.setattr(scheduler_module, "save_task", lambda task: True)
    monkeypatch.setattr(scheduler_module, "retire_task", lambda task: None)
    scheduler = EvalScheduler()
    release = {"a": asyncio.Event(), "b": asyncio.Event()}
    started = {"a": asyncio.Event(), "b": asyncio.Event()}
    events: list[str] = []
    active = 0
    max_active = 0

    async def runner(task, _cfg):
        nonlocal active, max_active
        task.status = "running"
        active += 1
        max_active = max(max_active, active)
        events.append(f"start:{task.id}")
        started[task.id].set()
        try:
            await release[task.id].wait()
            task.status = "done"
            events.append(f"done:{task.id}")
        finally:
            active -= 1
            task.active_runs -= 1

    task_a = _task("a", 15)
    task_b = _task("b", 10)
    assert scheduler.enqueue(task_a, object(), runner) == 1
    assert scheduler.enqueue(task_b, object(), runner) == 2

    await asyncio.wait_for(started["a"].wait(), timeout=1)
    assert not started["b"].is_set()
    snapshot = scheduler.snapshot()
    assert snapshot["running"]["task_id"] == "a"
    assert snapshot["running"]["concurrency"] == 15
    assert snapshot["queued"] == [
        {
            "job_id": "b",
            "kind": "initial",
            "task_id": "b",
            "dataset_name": "dataset-b",
            "mode": "rich_content",
            "status": "queued",
            "concurrency": 10,
            "done": 0,
            "total": 1,
            "created_at": task_b.created_at,
            "queue_position": 1,
        }
    ]

    release["a"].set()
    await asyncio.wait_for(started["b"].wait(), timeout=1)
    assert events[:3] == ["start:a", "done:a", "start:b"]
    assert max_active == 1

    release["b"].set()
    for _ in range(20):
        if scheduler.snapshot() == {"running": None, "queued": []}:
            break
        await asyncio.sleep(0)
    assert events == ["start:a", "done:a", "start:b", "done:b"]
    assert scheduler.snapshot() == {"running": None, "queued": []}
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_continues_after_unexpected_runner_failure(monkeypatch):
    monkeypatch.setattr(scheduler_module, "save_task", lambda task: True)
    monkeypatch.setattr(scheduler_module, "retire_task", lambda task: None)
    scheduler = EvalScheduler()
    second_finished = asyncio.Event()

    async def runner(task, _cfg):
        if task.id == "a":
            raise RuntimeError("boom")
        task.status = "done"
        task.active_runs -= 1
        second_finished.set()

    task_a = _task("a", 1)
    task_b = _task("b", 1)
    scheduler.enqueue(task_a, object(), runner)
    scheduler.enqueue(task_b, object(), runner)

    await asyncio.wait_for(second_finished.wait(), timeout=1)
    assert task_a.status == "error"
    assert task_a.active_runs == 0
    assert task_b.status == "done"
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_cancels_only_queued_task_and_compacts_positions(monkeypatch):
    monkeypatch.setattr(scheduler_module, "save_task", lambda task: True)
    retired: list[str] = []
    monkeypatch.setattr(scheduler_module, "retire_task", lambda task: retired.append(task.id))
    scheduler = EvalScheduler()
    release_a = asyncio.Event()
    started_a = asyncio.Event()

    async def runner(task, _cfg):
        task.status = "running"
        started_a.set()
        try:
            await release_a.wait()
            task.status = "done"
        finally:
            task.active_runs -= 1

    task_a = _task("a", 2)
    task_b = _task("b", 3)
    task_c = _task("c", 4)
    scheduler.enqueue(task_a, object(), runner)
    scheduler.enqueue(task_b, object(), runner)
    scheduler.enqueue(task_c, object(), runner)
    await asyncio.wait_for(started_a.wait(), timeout=1)

    assert scheduler.cancel("a") is None
    assert scheduler.cancel("b") is task_b
    assert task_b.status == "cancelled"
    assert task_b.active_runs == 0
    await scheduler_module.drain_task_saves()
    await asyncio.sleep(0)
    assert retired == ["b"]
    assert [entry["task_id"] for entry in scheduler.snapshot()["queued"]] == ["c"]
    assert scheduler.snapshot()["queued"][0]["queue_position"] == 1

    # 避免测试结束时 C 真正启动；先把它也从等待队列取消。
    assert scheduler.cancel("c") is task_c
    release_a.set()
    for _ in range(20):
        if scheduler.snapshot() == {"running": None, "queued": []}:
            break
        await asyncio.sleep(0)
    await scheduler.stop()


def test_queued_snapshot_is_treated_as_interrupted_after_restart():
    assert _apply_interrupted_status("queued", None) == (
        "error",
        "服务中断，已保留中断前完成的评估结果",
    )


def test_frontend_allows_submit_while_another_task_is_active():
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/auto_eval/web/static/index.html").read_text(encoding="utf-8")
    js = (root / "src/auto_eval/web/static/app.js").read_text(encoding="utf-8")

    assert ':disabled="submitting || !canSubmit"' in html
    assert ':disabled="running || !canSubmit"' not in html
    assert 'fetch("/api/queue")' in js
    assert "activeEventSource" in js
    assert "cancelQueuedTask" in js
    assert "reprioritizeQueuedTask" in js
    assert "move_to_front" in html
    assert "取消排队" in html
    assert "置顶" in html


@pytest.mark.asyncio
async def test_scheduler_can_promote_only_waiting_jobs(monkeypatch):
    monkeypatch.setattr(scheduler_module, "save_task", lambda task: True)
    monkeypatch.setattr(scheduler_module, "retire_task", lambda task: None)
    scheduler = EvalScheduler()
    release = asyncio.Event()
    started = asyncio.Event()

    async def runner(task, _cfg):
        task.status = "running"
        started.set()
        try:
            await release.wait()
            task.status = "done"
        finally:
            task.active_runs -= 1

    tasks = [_task(name, 1) for name in ("a", "b", "c", "d")]
    for task in tasks:
        scheduler.enqueue(task, object(), runner)
    await asyncio.wait_for(started.wait(), timeout=1)

    assert scheduler.reprioritize("a", "move_to_front") is None
    assert scheduler.reprioritize("d", "move_up") == 2
    assert [row["job_id"] for row in scheduler.snapshot()["queued"]] == ["b", "d", "c"]
    assert scheduler.reprioritize("d", "move_to_front") == 1
    assert [row["job_id"] for row in scheduler.snapshot()["queued"]] == ["d", "b", "c"]

    for name in ("d", "b", "c"):
        assert scheduler.cancel(name) is not None
    release.set()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_retry_job_uses_same_queue_without_reopening_parent(monkeypatch):
    monkeypatch.setattr(scheduler_module, "save_task", lambda task: True)
    monkeypatch.setattr(scheduler_module, "retire_task", lambda task: None)
    scheduler = EvalScheduler()
    parent = _task("parent", 2)
    parent.status = "done"
    parent.retry_runs["retry_1"] = {"retry_id": "retry_1", "status": "queued", "completed": 0}
    release = asyncio.Event()
    started = asyncio.Event()

    async def retry_runner(task, _cfg):
        started.set()
        await release.wait()
        task.retry_runs["retry_1"]["status"] = "completed"
        task.active_runs -= 1

    assert scheduler.enqueue_retry(
        parent, object(), retry_runner, retry_id="retry_1", total=1
    ) == 1
    await asyncio.wait_for(started.wait(), timeout=1)
    assert parent.status == "done"
    snapshot = scheduler.snapshot()
    assert snapshot["running"]["kind"] == "retry"
    release.set()
    for _ in range(20):
        if scheduler.snapshot() == {"running": None, "queued": []}:
            break
        await asyncio.sleep(0)
    assert parent.status == "done"
    assert parent.retry_runs["retry_1"]["status"] == "completed"
    await scheduler.stop()
