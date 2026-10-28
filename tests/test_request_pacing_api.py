"""Request-pacing API exposes global limiter stats without credentials."""
from collections import OrderedDict
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.judge_profiles import resolve_new_runtime
from auto_eval.web import scheduler as scheduler_module, server, tasks


@pytest.mark.asyncio
async def test_request_pacing_returns_limiter_stats_without_credentials(monkeypatch):
    model_stats = {"limit": 5, "running": 2, "queued": 1, "max_in_flight": 40}
    pipeline_stats = {"limit": 50, "running": 8, "queued": 2}

    monkeypatch.setattr(server, "MODEL_LIMITER", SimpleNamespace(stats=lambda: model_stats))
    monkeypatch.setattr(server, "PIPELINE_LIMITER", SimpleNamespace(stats=lambda: pipeline_stats))
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            concurrency=5, max_in_flight=40, waiting_capacity=10, eval_timeout_s=900.0,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/request-pacing")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["active"] is True
    assert payload["model_limiter"] == model_stats
    assert payload["pipeline_limiter"] == pipeline_stats
    assert payload["settings"]["concurrency"] == 5
    assert payload["settings"]["max_in_flight"] == 40
    assert payload["settings"]["eval_timeout_s"] == 900.0
    assert "api_key" not in response.text
    assert "dashscope" not in response.text


@pytest.fixture
def queued_api(monkeypatch):
    config = AppConfig(judges=[JudgeConfig(
        name="judge_2", model="fake", base_url="https://judge.invalid/v1",
    )])
    scheduler = scheduler_module.EvalScheduler()
    monkeypatch.setattr(scheduler, "start", lambda: None)
    monkeypatch.setattr(scheduler_module, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(scheduler_module, "get_settings", lambda: SimpleNamespace(concurrency=7))
    monkeypatch.setattr(server, "EVAL_SCHEDULER", scheduler)
    monkeypatch.setattr(server, "cfg", lambda: config)
    monkeypatch.setattr(server, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(server, "spawn_background", lambda coroutine: coroutine.close())
    monkeypatch.setattr(tasks, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "TASKS", OrderedDict())

    async def sources(*args):
        return []

    async def get_task(task_id):
        return tasks.TASKS.get(task_id)

    monkeypatch.setattr(server, "_request_source_files", sources)
    monkeypatch.setattr(server, "get_task_async", get_task)
    return config, scheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "bad", {}, True, 15])
@pytest.mark.parametrize("route", ["eval", "items", "retry"])
async def test_deprecated_task_capacity_is_ignored_by_submission_and_queue(queued_api, value, route):
    config, scheduler = queued_api
    options = {"concurrency": value, "eval_timeout_s": 321}
    payload = {"mode": "rich_content", "items": [{"id": "one", "query": "test"}], "options": options}
    url = "/api/eval"
    if route == "items":
        payload["task_id"] = "created"
        url += "/items"
    elif route == "retry":
        runtime, _ = resolve_new_runtime(config, {})
        task = tasks.Task(
            id="existing", mode="rich_content", items=payload["items"],
            options={"concurrency": value}, judge_runtime=runtime,
            status="done", results=[{"index": 0, "error": "fake failure"}],
        )
        tasks.TASKS[task.id] = task
        payload = {"options": options}
        url += f"/{task.id}/retries"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.post(url, json=payload)
        assert response.is_success, response.text
        task = tasks.TASKS[response.json()["task_id"]]
        stored = task.retry_runs[response.json()["retry_id"]]["options"] if route == "retry" else task.options
        assert "concurrency" not in stored
        assert stored["eval_timeout_s"] == 321
        if route == "items":
            scheduler.enqueue(task, config, server.run_eval)
        queue = await client.get("/api/queue")

    assert queue.status_code == 200
    assert queue.json()["queued"][0]["concurrency"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "retry", "resume"])
@pytest.mark.parametrize("value", [None, "bad", {}, True])
async def test_queue_ignores_historical_task_retry_and_resume_capacity(queued_api, kind, value):
    _, scheduler = queued_api
    task = tasks.Task(id="legacy", mode="rich_content", items=[{"query": "test"}], options={"concurrency": value})
    if kind == "retry":
        task.retry_runs["retry"] = {"options": {"concurrency": value}}
    elif kind == "resume":
        task.execution_control["resumes"] = [{"status": "queued", "concurrency": value}]
    scheduler._running = scheduler_module._QueuedEval(
        job_id="retry" if kind == "retry" else task.id,
        kind="retry" if kind == "retry" else "initial",
        task=task, cfg=None, runner=server.run_eval, total=1,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/queue")

    assert response.status_code == 200
    running = response.json()["running"]
    assert running["kind"] == kind
    assert running["concurrency"] == 7
    assert task.options["concurrency"] == value
