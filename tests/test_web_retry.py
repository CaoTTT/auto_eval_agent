from __future__ import annotations

import time
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from auto_eval.web import runner as runner_module
from auto_eval.web import server as server_module
from auto_eval.web.tasks import Task, latest_results_by_index


def _retry_record(indexes: list[int]) -> dict:
    return {
        "retry_id": "retry_test",
        "status": "queued",
        "indexes": indexes,
        "reasons": {str(index): "failed" for index in indexes},
        "options": {},
        "total": len(indexes),
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "items": {str(index): {"status": "queued"} for index in indexes},
        "created_at": time.time(),
    }


@pytest.mark.asyncio
async def test_retry_writes_back_only_successful_candidates(monkeypatch):
    task = Task(
        id="parent",
        mode="rich_content",
        items=[
            {"id": "a", "query": "a", "context": ""},
            {"id": "b", "query": "b", "context": ""},
        ],
        options={},
        status="done",
        results=[
            {"index": 0, "item_id": "a", "error": "old-a"},
            {"index": 1, "item_id": "b", "error": "old-b"},
        ],
        active_runs=1,
    )
    task.retry_runs["retry_test"] = _retry_record([0, 1])

    def fake_make(_task, _cfg, *, options, on_result):
        async def one(index, item):
            started = time.perf_counter()
            result = (
                {"index": index, "item_id": item["id"], "query": item["query"], "answer_coverage": "complete"}
                if index == 0
                else {"index": index, "item_id": item["id"], "query": item["query"], "error": "new-b"}
            )
            await on_result(index, result, started)
            return result

        return one, []

    monkeypatch.setattr(runner_module, "_make_item_evaluator", fake_make)
    monkeypatch.setattr(runner_module, "save_task", lambda _task: True)
    monkeypatch.setattr(runner_module, "retire_task", lambda _task: None)

    await runner_module.run_retry(task, object(), "retry_test")

    latest = latest_results_by_index(task)
    assert latest[0].get("error") is None
    assert latest[0]["retry_id"] == "retry_test"
    assert latest[1]["error"] == "old-b"
    retry = task.retry_runs["retry_test"]
    assert retry["status"] == "partial"
    assert retry["succeeded"] == 1
    assert retry["failed"] == 1
    assert task.summary["total"] == 2
    assert task.summary["done"] == 1
    assert task.summary["failed"] == 1
    assert task.active_runs == 0


@pytest.mark.asyncio
async def test_retry_endpoint_selects_only_failed_rows(monkeypatch):
    task = Task(
        id="parent",
        mode="rich_content",
        items=[{"id": "a", "query": "a"}, {"id": "b", "query": "b"}],
        options={"concurrency": 4},
        status="done",
        results=[
            {"index": 0, "item_id": "a", "error": "provider unavailable"},
            {"index": 1, "item_id": "b", "answer_coverage": "complete"},
        ],
    )
    captured = {}

    async def fake_get(_task_id):
        return task

    class FakeScheduler:
        def enqueue_retry(self, parent, cfg, runner, *, retry_id, total):
            captured.update({"task": parent, "retry_id": retry_id, "total": total})
            parent.repair_status = "queued"
            parent.active_runs += 1
            return 3

    monkeypatch.setattr(server_module, "get_task_async", fake_get)
    monkeypatch.setattr(server_module, "EVAL_SCHEDULER", FakeScheduler())
    monkeypatch.setattr(server_module, "cfg", lambda: object())

    response = await server_module.api_retry_failed(
        "parent",
        server_module.RetryReq(indexes=None, idempotency_key="once"),
    )

    assert response["accepted_indexes"] == [0]
    assert response["selected"] == 1
    assert response["queue_position"] == 3
    assert captured["total"] == 1

    replay = await server_module.api_retry_failed(
        "parent",
        server_module.RetryReq(indexes=None, idempotency_key="once"),
    )
    assert replay["retry_id"] == response["retry_id"]
    assert replay["idempotent_replay"] is True


def _parse_sse(chunk):
    if isinstance(chunk, bytes):
        chunk = chunk.decode()
    lines = chunk.strip().splitlines()
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_mode,count", [("long_screenshot", 2), ("long_screenshot", 3), ("video_frames", 2), ("video_frames", 3)])
async def test_compact_replay_batches_logs_and_preserves_retry_progress(monkeypatch, evidence_mode, count):
    task = Task(id="replay", mode="compare", items=[{"query": str(i), "evidence_mode": evidence_mode, "product_count": count} for i in range(15)], options={}, status="done", active_runs=1)
    task.results = [{"index": i, "error": "old failure"} for i in range(15)]
    task.repair_status = "running"
    for i in range(15):
        for sequence in range(100):
            runner_module._record_progress(task, i, {"item_index": i, "request_id": f"retry-{i}", "status": "running", "percent": 5})
    monkeypatch.setattr(server_module, "peek_task", lambda _id: task)
    response = await server_module.api_stream(task.id, compact=True)
    stream = response.body_iterator
    event, state = _parse_sse(await anext(stream))
    assert event == "replay_state"
    assert state["results"][0]["error"] == "old failure"
    assert state["item_progress"]["0"]["status"] == "running"
    assert state["repair_status"] == "running"
    # An event arriving while the history is being sent must follow that history.
    runner_module._record_progress(task, 0, {"item_index": 0, "request_id": "retry-0", "status": "running", "percent": 20})
    for i in range(15):
        event, history = _parse_sse(await anext(stream))
        assert event == "progress_history"
        assert history["item_index"] == i
        assert [row["sequence"] for row in history["events"]] == list(range(1, 101))
    event, update = _parse_sse(await asyncio.wait_for(anext(stream), 1))
    assert event == "item_progress"
    assert update["sequence"] == 101
    assert update["percent"] == 20
    await stream.aclose()
    assert not task.subscribers


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_replay_historical_task_remains_compatible(monkeypatch, compact):
    task = Task(id="old", mode="rich_content", items=[{"query": "video"}], options={}, status="done", results=[{"index": 0, "error": "old failure"}])
    runner_module._record_progress(task, 0, {"item_index": 0, "status": "error"})
    monkeypatch.setattr(server_module, "peek_task", lambda _id: task)
    response = await server_module.api_stream(task.id, compact=compact)
    events = [_parse_sse(chunk)[0] async for chunk in response.body_iterator]
    assert events == (["replay_state", "progress_history", "done"] if compact else ["progress_event", "item_progress", "result", "done"])
    assert not task.subscribers


def test_retry_progress_does_not_inherit_previous_attempt_start():
    task = Task(id="clock", mode="compare", items=[], options={})
    runner_module._record_progress(task, 0, {"request_id": "old", "started_at": 100})
    queued = runner_module._record_progress(task, 0, {"request_id": "retry"})
    assert "started_at" not in queued
    runner_module._record_progress(task, 0, {"request_id": "retry", "started_at": 200})
    running = runner_module._record_progress(task, 0, {"request_id": "retry"})
    assert running["started_at"] == 200


def test_web_retry_progress_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend regression harness")
    completed = subprocess.run([node, str(Path(__file__).with_name("test_web_retry_progress.cjs"))], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
