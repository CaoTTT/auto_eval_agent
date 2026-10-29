"""Interrupted retry/resume working copies retain no remote media downloads."""
import asyncio
from pathlib import Path

import pytest

from auto_eval.config import load_config
from auto_eval.remote_media import TEMPORARY_DOWNLOAD_PATHS_FIELD, cleanup_item_downloads
from auto_eval.web import runner
from auto_eval.web.tasks import Task


@pytest.mark.parametrize("execution", ["resume", "retry"])
@pytest.mark.parametrize("interruption", ["pause", "cancel"])
async def test_interrupted_working_copy_releases_downloads(
    tmp_path, monkeypatch, execution, interruption,
):
    task = Task(
        id=f"remote-cleanup-{execution}-{interruption}", mode="compare",
        items=[{"id": "q1", "query": "q", "product_count": 2,
                "video1": "https://example.test/one.mp4",
                "video2": "https://example.test/two.mp4"}],
        options={}, active_runs=1,
    )
    downloads = tmp_path / "runs" / "downloads"
    downloads.mkdir(parents=True)
    downloaded = downloads / "one.mp4"

    class Client:
        def __init__(self, cfg):
            self.config = cfg

        async def aclose(self):
            pass

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "RichContentJudge", lambda *a: None)
    monkeypatch.setattr(runner, "VisualCompareJudge", lambda *a: None)
    monkeypatch.setattr(runner, "_persist_task", lambda *a, **k: None)
    monkeypatch.setattr(runner, "retire_task", lambda _: None)

    async def persist(*args, **kwargs):
        return True

    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "cleanup_item_downloads", lambda item: cleanup_item_downloads(
        item, runs_dir=tmp_path / "runs",
    ))

    def prepare(item, **kwargs):
        # The evaluator receives a deep copy in both entry points.
        assert item is not task.items[0]
        downloaded.write_bytes(b"remote-video")
        prepared = {**item, "frames1": ["a"], "frames2": ["b"],
                    TEMPORARY_DOWNLOAD_PATHS_FIELD: [str(downloaded)]}
        if interruption == "pause":
            task.execution_control["state"] = "pausing"
        return prepared

    async def evaluate(*args, **kwargs):
        if interruption == "cancel":
            raise asyncio.CancelledError
        pytest.fail("A paused item must not dispatch a model request")

    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    cfg = load_config(Path(__file__).parents[1] / "config")
    if execution == "resume":
        task.execution_control.update(state="queued", pending_indexes=[0], resumes=[{
            "id": "resume", "indexes": [0], "completed": 0, "failed": 0,
        }])
        operation = runner.run_resume(task, cfg)
    else:
        task.retry_runs["retry"] = {
            "indexes": [0], "items": {}, "options": {}, "completed": 0,
            "failed": 0, "skipped": 0, "succeeded": 0,
        }
        operation = runner.run_retry(task, cfg, "retry")

    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation
        assert task.status == "paused"
    assert not downloaded.exists()
    assert TEMPORARY_DOWNLOAD_PATHS_FIELD not in task.items[0]


async def test_full_run_does_not_remove_another_active_batch_download(tmp_path, monkeypatch):
    downloaded = tmp_path / "downloads" / "other-batch.mp4"
    downloaded.parent.mkdir()
    downloaded.write_bytes(b"other-batch")
    task = Task(
        id="remote-concurrent-cleanup", mode="compare", options={}, active_runs=2,
        items=[{"id": "q1", "query": "q", TEMPORARY_DOWNLOAD_PATHS_FIELD: [str(downloaded)]}],
    )

    async def finished(*args, **kwargs):
        return True

    monkeypatch.setattr(runner, "_run", finished)
    monkeypatch.setattr(runner, "_persist_task_and_wait", finished)
    monkeypatch.setattr(runner, "_summarize", lambda _: {})
    monkeypatch.setattr(runner, "retire_task", lambda _: None)
    monkeypatch.setattr(runner, "cleanup_item_downloads", lambda item: cleanup_item_downloads(
        item, runs_dir=tmp_path,
    ))

    await runner.run_eval(task, object())

    assert task.active_runs == 1
    assert downloaded.is_file()
    assert task.items[0][TEMPORARY_DOWNLOAD_PATHS_FIELD] == [str(downloaded)]
