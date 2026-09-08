from __future__ import annotations

import time

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
