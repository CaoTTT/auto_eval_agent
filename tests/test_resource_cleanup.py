"""Resource lifetime regressions, using weak references and fake model calls."""
import asyncio
import gc
import json
import threading
import weakref
import subprocess
import sys
from types import SimpleNamespace

import pytest

from auto_eval.web import runner, scheduler as sched, tasks, persistence, exports
from auto_eval import preparation, media
from auto_eval import long_screenshot
from auto_eval.config import LongScreenshotConfig
from auto_eval.web import video_prepare
from PIL import Image


async def settle():
    await persistence.drain_task_saves()
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()


def fake_saves(monkeypatch):
    for module in (runner, sched, tasks):
        monkeypatch.setattr(module, "save_task", lambda _: True)
    monkeypatch.setattr(tasks, "TASKS", tasks.OrderedDict())


@pytest.mark.asyncio
async def test_completed_jobs_released_by_idle_scheduler(monkeypatch):
    fake_saves(monkeypatch)
    closed, refs = [], []
    class Client:
        async def aclose(self):
            closed.append(True)
    def make(task, cfg):
        async def one(index, item):
            task.results.append({"index": index})
        return one, [Client()]
    monkeypatch.setattr(runner, "_make_item_evaluator", make)
    scheduler = sched.EvalScheduler()
    try:
        for index in range(30):
            task = tasks.new_task("compare", [{"query": "x" * 100_000}], {}, task_id=f"normal-{index}")
            refs.append(weakref.ref(task))
            scheduler.enqueue(task, None, runner.run_eval)
            del task
            for _ in range(2000):
                if not scheduler._pending and scheduler._running is None:
                    break
                await asyncio.sleep(.001)
            else:
                pytest.fail("scheduler did not become idle")
            await settle()
        live = [i for i, ref in enumerate(refs) if ref() is not None]
        print(json.dumps({"normal_runs": 30, "closed_clients": len(closed), "registry": len(tasks.TASKS), "retained_task_indexes": live, "flush_timers": len(runner._pending_flush)}))
        assert len(closed) == 30
        assert not tasks.TASKS
        assert live == []
    finally:
        await scheduler.stop()
    await settle()
    assert not any(ref() for ref in refs)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["initial", "retry"])
async def test_cancelled_queue_released_after_all_saves(monkeypatch, kind):
    fake_saves(monkeypatch)
    scheduler = sched.EvalScheduler()
    started, release = asyncio.Event(), asyncio.Event()
    async def blocker(task, cfg):
        started.set()
        await release.wait()
        task.active_runs -= 1
        task.status = "done"
        await persistence.wait_task_save(task, save=lambda _: True)
        tasks.retire_task(task)
    first = tasks.new_task("compare", [], {}, task_id="blocker")
    scheduler.enqueue(first, None, blocker)
    await started.wait()
    refs = []
    try:
        for i in range(24):
            task = tasks.new_task("compare", [{"query": "y" * 100_000}], {}, task_id=f"cancel-{i}")
            refs.append(weakref.ref(task))
            if kind == "retry":
                task.status = "done"
                scheduler.enqueue_retry(task, None, blocker, retry_id=task.id, total=1)
            else:
                scheduler.enqueue(task, None, blocker)
        del task
        for i in range(24):
            assert scheduler.cancel(f"cancel-{i}") is not None
        await settle()
        release.set()
        for _ in range(2000):
            if scheduler._running is None:
                break
            await asyncio.sleep(.001)
        await settle()
        retained = len([ref for ref in refs if ref() is not None])
        print(json.dumps({"cancelled_runs": 24, "retained_cancelled": retained, "registry": len(tasks.TASKS), "configured_capacity": tasks.TASKS_CAPACITY, "pending_saves": sum(persistence.task_save_pending(f"cancel-{i}") for i in range(24))}))
        assert retained == 0
        assert len(tasks.TASKS) == 0
        assert not any(persistence.task_save_pending(f"cancel-{i}") for i in range(24))
    finally:
        release.set()
        await scheduler.stop()
    tasks.TASKS.clear()
    await settle()
    assert not any(ref() for ref in refs)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_runner_closes_clients_and_clears_registry(monkeypatch, outcome):
    fake_saves(monkeypatch)
    closed, started = [], asyncio.Event()
    class Client:
        async def aclose(self):
            closed.append(True)
    def make(task, cfg):
        async def one(index, item):
            started.set()
            if outcome == "error":
                raise RuntimeError("synthetic item error")
            if outcome == "cancel":
                await asyncio.Event().wait()
        return one, [Client()]
    monkeypatch.setattr(runner, "_make_item_evaluator", make)
    task = tasks.new_task("compare", [{"query": "probe"}], {})
    task.active_runs = 1
    ref = weakref.ref(task)
    job = runner.spawn_background(runner.run_eval(task, None))
    await started.wait()
    if outcome == "cancel":
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
    else:
        await job
    assert closed == [True]
    assert task.active_runs == 0
    assert task.id not in tasks.TASKS
    del task, job
    await settle()
    assert not runner._BACKGROUND_TASKS
    assert not runner._pending_flush
    assert ref() is None


@pytest.mark.asyncio
async def test_cancellation_waits_for_current_atomic_operation_and_handles_repeat_cancel():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def work():
        started.set()
        release.wait(5)
        finished.set()
    job = asyncio.create_task(preparation.run_preparation(work, timeout=5))
    while not started.is_set():
        await asyncio.sleep(.001)
    try:
        job.cancel()
        await asyncio.sleep(.01)
        job.cancel()
        await asyncio.sleep(.01)
        assert not job.done()
        assert not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert finished.is_set()


@pytest.mark.asyncio
async def test_export_jobs_release_loaded_task_and_workers(monkeypatch, tmp_path):
    refs = []
    async def load(task_id):
        task = tasks.Task(id=task_id, mode="compare", items=[{"query": "z" * 100_000}], options={}, status="done")
        refs.append(weakref.ref(task))
        return task
    monkeypatch.setattr(exports, "peek_task_async", load)
    manager = exports.XlsxExports(tmp_path)
    monkeypatch.setattr(manager, "_write", lambda snapshot, path: path.write_bytes(b"synthetic"))
    for i in range(30):
        manager.create(str(i))
        await asyncio.gather(*list(manager.workers))
        await settle()
    assert not manager.workers
    assert not any(ref() for ref in refs)
    print(json.dumps({"exports": 30, "workers": len(manager.workers), "retained_export_tasks": 0, "small_download_records": len(manager.jobs)}))
    assert len(manager.jobs) <= 16
    await manager.close()
    assert not manager.jobs
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_mode", ["long_screenshot", "video"])
@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_real_runner_stops_preparation_thread(monkeypatch, evidence_mode, outcome):
    fake_saves(monkeypatch)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed = []
    class Client:
        def __init__(self, cfg):
            pass
        async def aclose(self):
            closed.append(True)
    def prepare(*args, **kwargs):
        started.set()
        try:
            while not release.wait(.005):
                preparation.check_preparation()
            return args[0]
        finally:
            finished.set()
    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "RichContentJudge", lambda *args: None)
    monkeypatch.setattr(runner, "VisualCompareJudge", lambda *args: None)
    monkeypatch.setattr(runner, "prepare_session_long_screenshot_item", prepare)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", prepare)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *args, **kwargs: None)
    cfg = SimpleNamespace(judges=[SimpleNamespace(name="fake", display="fake")], visual_modes={"rich_content": SimpleNamespace(category_display={})})
    task = tasks.new_task("compare", [{"query": "probe", "evidence_mode": evidence_mode}], {"video_prepare_timeout_s": .1 if outcome == "timeout" else 5})
    task.active_runs = 1
    ref = weakref.ref(task)
    try:
        job = asyncio.create_task(runner.run_eval(task, cfg))
        if outcome == "cancel":
            while not started.is_set():
                await asyncio.sleep(.001)
            job.cancel()
            with pytest.raises(asyncio.CancelledError):
                await job
        else:
            await job
            assert task.results[0].get("error")
        assert started.is_set() and finished.is_set()
        assert task.active_runs == 0 and task.id not in tasks.TASKS
        assert closed == [True]
        del task, job
        await settle()
        assert ref() is None
    finally:
        release.set()
    while not finished.is_set():
        await asyncio.sleep(.001)
    await settle()
    assert ref() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["probe", "scene", "frame", "command"])
@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_media_subprocess_reaped_even_when_pipe_stalls(monkeypatch, tmp_path, operation, outcome):
    processes = []
    real_popen = subprocess.Popen
    def popen(command, **kwargs):
        # A real stalled child, independent of installed ffmpeg/ffprobe.
        proc = real_popen([sys.executable, "-c", "import sys,time; print('pts_time:1.0',file=sys.stderr,flush=True); time.sleep(60)"], **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(preparation.subprocess, "Popen", popen)
    operations = {
        "probe": lambda: media.probe_duration(tmp_path / "video.mp4"),
        "scene": lambda: media.scene_change_times(tmp_path / "video.mp4"),
        "frame": lambda: media._extract_at(tmp_path / "video.mp4", 1, tmp_path / "frame.jpg", max_edge=240),
        "command": lambda: preparation.run_media_command(["unused"], capture_output=True),
    }
    job = asyncio.create_task(preparation.run_preparation(operations[operation], timeout=.2 if outcome == "timeout" else 5))
    try:
        if outcome == "cancel":
            while not processes:
                await asyncio.sleep(.001)
            job.cancel()
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else TimeoutError):
            await job
        assert len(processes) == 1
        assert processes[0].poll() is not None
        assert all(pipe is None or pipe.closed for pipe in (processes[0].stdout, processes[0].stderr))
        assert not any(t.name == "media-process-watch" for t in threading.enumerate())
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def test_media_command_normal_output_failure_and_standalone_deadline():
    result = preparation.run_media_command([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "ok" and result.returncode == 0
    with pytest.raises(subprocess.CalledProcessError):
        preparation.run_media_command([sys.executable, "-c", "raise SystemExit(3)"], check=True)
    with pytest.raises(preparation.PreparationStopped):
        preparation.run_media_command([sys.executable, "-c", "import time; time.sleep(60)"], timeout=.1)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["boundary_signals", "_optimal_path", "_png_bytes"])
async def test_screenshot_cancellation_closes_image_and_stops_pipeline(monkeypatch, tmp_path, stage):
    path = tmp_path / "long.png"
    with Image.new("RGB", (32, 512), "white") as image:
        image.save(path)
    images = []
    read = long_screenshot._read_image
    def capture(path):
        result = read(path)
        images.append(result[1])
        return result
    original = getattr(long_screenshot, stage)
    def cancel_at_stage(*args, **kwargs):
        preparation._scope.get().cancelled.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(long_screenshot, "_read_image", capture)
    monkeypatch.setattr(long_screenshot, stage, cancel_at_stage)
    with pytest.raises(preparation.PreparationStopped):
        await preparation.run_preparation(long_screenshot.prepare_long_screenshot, path, tmp_path / "parts", LongScreenshotConfig(max_pixels=32 * 256), timeout=5)
    assert len(images) == 1
    with pytest.raises(ValueError, match="closed"):
        images[0].getpixel((0, 0))
    assert not list((tmp_path / "parts").glob("*.png"))


@pytest.mark.asyncio
async def test_cancelled_video_does_not_publish_complete_cache(tmp_path):
    frames_dir = tmp_path / "frames"
    def extract(*args, **kwargs):
        path = frames_dir / "kf_001.jpg"
        path.write_bytes(b"partial")
        preparation._scope.get().cancelled.set()
        return [path]
    with pytest.raises(preparation.PreparationStopped):
        await preparation.run_preparation(video_prepare._extract_frames, tmp_path / "video.mp4", frames_dir, extract_fn=extract, timeout=5)
    assert not (frames_dir / ".complete").exists()


@pytest.mark.asyncio
async def test_cancel_retirement_preserves_resubmitted_task_and_failed_save(monkeypatch):
    fake_saves(monkeypatch)
    scheduler = sched.EvalScheduler()
    monkeypatch.setattr(scheduler, "start", lambda: None)
    task = tasks.new_task("compare", [], {}, task_id="reuse")
    async def unused(*args):
        pass
    scheduler.enqueue(task, None, unused)
    scheduler.cancel(task.id)
    scheduler.enqueue(task, None, unused)
    await settle()
    assert tasks.TASKS[task.id] is task and task.active_runs == 1
    assert task.status == "queued"
    monkeypatch.setattr(sched, "save_task", lambda _: False)
    scheduler.cancel(task.id)
    await settle()
    assert tasks.TASKS[task.id] is task  # Failed persistence must not lose the only copy.
    await scheduler.stop()
