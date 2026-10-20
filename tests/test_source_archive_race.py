"""Task-source archiving pins its target until the event-loop mutation is ready."""
import asyncio
from collections import OrderedDict
import threading

import httpx
import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.web import history, server, tasks


@pytest.fixture
def archive_api(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    registry = OrderedDict()
    monkeypatch.setattr(tasks, "TASKS", registry)
    monkeypatch.setattr(server, "TASKS", registry)
    monkeypatch.setattr(server, "_source_archive_pending", set())
    app_cfg = AppConfig(
        judges=[JudgeConfig(name="judge_2", model="fake")],
        visual_modes={"rich_content": VisualModeProfile(extraction={"algorithm_version": "test"})},
    )
    monkeypatch.setattr(server, "cfg", lambda: app_cfg)

    def save(task, **kwargs):
        assert history.save_task(task)

    monkeypatch.setattr(tasks, "queue_task_save", save)
    monkeypatch.setattr(server, "queue_task_save", save)
    monkeypatch.setattr(server, "spawn_background", lambda coroutine: coroutine.close())
    return registry


def update_request(task_id, **extra):
    return {"task_id": task_id, "mode": "rich_content",
            "items": [{"id": "one", "query": "question"}], **extra}


def hold_source_copy(monkeypatch):
    """Pause the worker after durable copy, before its event-loop continuation."""
    copied = asyncio.Event()
    resume = threading.Event()
    loop = asyncio.get_running_loop()
    original = server.prepare_task_sources

    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        loop.call_soon_threadsafe(copied.set)
        if not resume.wait(10):
            raise TimeoutError("test did not release source copy")
        return result

    monkeypatch.setattr(server, "prepare_task_sources", prepare)
    return copied, resume


@pytest.mark.asyncio
async def test_delete_cannot_remove_archives_before_update_is_pinned(archive_api, monkeypatch):
    task_id = "existing-target"
    judge_runtime, _ = server._new_judge_runtime(server.cfg(), {})
    task = tasks.Task(id=task_id, mode="rich_content", items=[], options={}, status="done",
                      judge_runtime=judge_runtime)
    archive_api[task_id] = task
    assert history.save_task(task)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        content = b"original uploaded bytes\r\n"
        source = (await client.post("/api/upload/dataset-source", files={"file": ("source.csv", content)})).json()
        copied, resume = hold_source_copy(monkeypatch)
        pending = asyncio.create_task(client.post("/api/eval/items", json=update_request(task_id, source_file_id=source["id"])))
        try:
            await asyncio.wait_for(copied.wait(), timeout=5)
            deleted = await client.delete(f"/api/history/{task_id}")
        finally:
            resume.set()
            updated = await pending
        assert deleted.status_code == 409, deleted.text
        assert updated.status_code == 200, updated.text
        assert updated.json()["created"] is False
        downloaded = await client.get(f"/api/history/{task_id}/source-files/{source['id']}")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == content
        assert not server._source_archive_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("second_source", [False, True])
async def test_upsert_reservation_rejects_overlapping_updates(archive_api, monkeypatch, second_source):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        source = (await client.post("/api/upload/dataset-source", files={"file": ("source.json", b"original")})).json()
        task_id = "new-target"
        copied, resume = hold_source_copy(monkeypatch)
        pending = asyncio.create_task(client.post("/api/eval/items", json=update_request(task_id, source_file_id=source["id"])))
        try:
            await asyncio.wait_for(copied.wait(), timeout=5)
            extra = {"source_file_id": source["id"]} if second_source else {}
            overlapping = await client.post("/api/eval/items", json=update_request(task_id, **extra))
        finally:
            resume.set()
            created = await pending
        assert overlapping.status_code == 409, overlapping.text
        assert created.status_code == 200, created.text
        assert created.json()["created"] is True
        assert archive_api[task_id].source_files == [source]
        assert not server._source_archive_pending


@pytest.mark.asyncio
async def test_source_preparation_failure_releases_target_reservation(archive_api):
    task_id = "retry-target"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        failed = await client.post("/api/eval/items", json=update_request(task_id, source_file_id="a" * 32))
        assert failed.status_code == 422, failed.text
        assert not server._source_archive_pending
        retried = await client.post("/api/eval/items", json=update_request(task_id))
        assert retried.status_code == 200, retried.text
        assert retried.json()["created"] is True
