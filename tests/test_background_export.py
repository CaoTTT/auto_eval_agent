import asyncio
import hashlib
import io
import os
import time
import threading
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from PIL import Image

from auto_eval.web import exports, history, persistence, runner, server
from auto_eval.web.tasks import Task


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), 3)


@pytest.mark.asyncio
async def test_snapshot_writes_are_ordered_frozen_coalesced_and_off_loop():
    task = Task(id="t", mode="compare", items=[{"nested": [0]}], options={})
    release, entered = threading.Event(), threading.Event()
    writes = []
    main_thread = threading.get_ident()
    def save(snapshot):
        assert threading.get_ident() != main_thread
        if not writes:
            entered.set()
            assert release.wait(3)
        writes.append((snapshot.items[0]["nested"][0], snapshot.status))
        return True
    first = persistence.queue_task_save(task, save=save)
    try:
        await wait_until(entered.is_set)
        assert persistence.task_save_pending(task.id)
        with pytest.raises(server.HTTPException) as deletion:
            await server.api_history_delete(task.id)
        assert deletion.value.status_code == 409
        task.items[0]["nested"][0] = 1
        second = persistence.queue_task_save(task, save=save)
        task.items[0]["nested"][0] = 2
        task.status = "done"
        last = persistence.queue_task_save(task, save=save)
        assert second is last  # 待写版本合并，数据和等待对象均不无限累积。
        assert not last.done()
    finally:
        release.set()
    assert await first
    assert await last
    await persistence.drain_task_saves()
    assert writes == [(0, "pending"), (2, "done")]
    assert not persistence.task_save_pending(task.id)


@pytest.mark.asyncio
async def test_snapshot_writer_recovers_after_failure():
    task = Task(id="t", mode="compare", items=[], options={})
    def fail(_):
        raise OSError("disk full")
    assert not await persistence.wait_task_save(task, save=fail)
    assert await persistence.wait_task_save(task, save=lambda _: True)
    await persistence.drain_task_saves()


@pytest.mark.asyncio
async def test_cancelled_evaluation_waits_for_terminal_snapshot(monkeypatch):
    task = Task(id="cancel", mode="compare", items=[], options={}, status="queued", active_runs=1)
    entered, release = threading.Event(), threading.Event()
    statuses = []
    retired = []
    def save(snapshot):
        if snapshot.status == "running":
            entered.set()
            assert release.wait(3)
        statuses.append(snapshot.status)
        return True
    monkeypatch.setattr(runner, "save_task", save)
    monkeypatch.setattr(runner, "retire_task", lambda task: retired.append(statuses[-1]))
    job = asyncio.create_task(runner.run_eval(task, object()))
    try:
        await wait_until(entered.is_set)
        job.cancel()
        await asyncio.sleep(.01)
        assert not job.done(), "cancellation must wait for the final save"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert task.active_runs == 0
    assert statuses[-1] == "error"
    assert retired == ["error"]
    await persistence.drain_task_saves()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,count", [("long_screenshot", 2), ("long_screenshot", 3), ("video_frames", 2), ("video_frames", 3)])
async def test_export_other_task_while_generation_is_busy(tmp_path, monkeypatch, mode, count):
    manager = exports.XlsxExports(tmp_path, capacity=2, ttl=1800)
    monkeypatch.setattr(server, "XLSX_EXPORTS", manager)
    item = {"query": "history", "product_count": count, "evidence_mode": mode}
    hashes = set()
    for n in range(1, count+1):
        path = tmp_path / f"{n}.png"
        Image.new("RGB", (40, 100), (n*30, 10, 20)).save(path)
        hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        item[f"screenshot{n}" if mode == "long_screenshot" else f"video{n}"] = str(path)
    historical = Task(id="B", mode="compare", status="done", items=[item], options={}, results=[{"index": 0, "error": "old failure"}])
    historical.dataset_name = "测试数据.v1.jsonl"
    running = Task(id="A", mode="compare", status="running", active_runs=1, items=[], options={})
    async def peek(key):
        return {"A": running, "B": historical}.get(key)
    monkeypatch.setattr(exports, "peek_task_async", peek)
    monkeypatch.setattr(server, "peek_task", lambda key, **kw: {"A": running, "B": historical}.get(key))
    release, entered = threading.Event(), threading.Event()
    original = manager._write
    def write(snapshot, path):
        entered.set()
        assert release.wait(3)
        original(snapshot, path)
    monkeypatch.setattr(manager, "_write", write)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        try:
            response = await client.post("/api/eval/B/exports")
            assert response.status_code == 202
            key = response.json()["export_id"]
            await wait_until(entered.is_set)
            assert (await client.get("/api/history/A")).json()["status"] == "running"
            assert (await client.get(f"/api/exports/{key}")).json()["status"] == "generating"
            assert (await client.get(f"/api/exports/{key}/download")).status_code == 409
            assert (await client.post("/api/eval/B/exports")).json()["export_id"] == key
            # Export is a frozen view; subsequent result changes cannot corrupt it.
            historical.items[0]["query"] = "changed later"
            historical.dataset_name = "另一个文件.jsonl"
        finally:
            release.set()
        await wait_until(lambda: manager.jobs[key]["status"] == "ready")
        download = await client.get(f"/api/exports/{key}/download")
        assert download.status_code == 200
        assert unquote(download.headers["content-disposition"]).endswith("测试数据.v1_模型测评结果.xlsx")
        assert (await client.get(f"/api/exports/{key}")).json()["filename"] == "测试数据.v1_模型测评结果.xlsx"
        archive = zipfile.ZipFile(io.BytesIO(download.content))
        assert archive.testzip() is None
        images = [name for name in archive.namelist() if name.startswith("xl/media/")]
        assert {hashlib.sha256(archive.read(name)).hexdigest() for name in images} == (hashes if mode == "long_screenshot" else set())
        assert b"changed later" not in b"".join(archive.read(name) for name in archive.namelist() if name.startswith("xl/worksheets/"))
        assert running.status == "running"
        manager.jobs[key]["finished"] -= 1801
        assert (await client.get(f"/api/exports/{key}")).status_code == 404
        assert not list(tmp_path.glob(".xlsx-*"))
    await manager.close()


@pytest.mark.asyncio
async def test_export_queue_capacity_failure_and_cleanup(tmp_path, monkeypatch):
    manager = exports.XlsxExports(tmp_path, capacity=1)
    task = Task(id="a", mode="compare", items=[], options={})
    async def peek(_):
        return task
    monkeypatch.setattr(exports, "peek_task_async", peek)
    def fail(snapshot, path):
        path.write_bytes(b"partial")
        raise OSError("disk full")
    monkeypatch.setattr(manager, "_write", fail)
    state = manager.create("a")
    with pytest.raises(server.HTTPException) as error:
        manager.create("b")
    assert error.value.status_code == 429
    await wait_until(lambda: manager.jobs[state["export_id"]]["status"] == "error")
    assert manager.view(state["export_id"])["error"]
    assert not list(tmp_path.glob(".xlsx-*"))
    await manager.close()


def test_file_writer_does_not_build_workbook_in_memory(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("file export must not allocate a whole workbook BytesIO")
    monkeypatch.setattr(history, "BytesIO", forbidden)
    path = tmp_path / "out.xlsx"
    history.write_xlsx({"mode": "compare", "items": [], "results": []}, path)
    assert zipfile.is_zipfile(path)


def test_export_cleanup_only_removes_owned_expired_files(tmp_path):
    old = tmp_path / (".xlsx-" + "a"*32 + ".xlsx")
    fresh = tmp_path / (".xlsx-" + "b"*32 + ".xlsx")
    unrelated = tmp_path / "customer.xlsx"
    for path in (old, fresh, unrelated):
        path.write_bytes(b"test")
    expired = time.time() - 3600
    os.utime(old, (expired, expired))
    os.utime(unrelated, (expired, expired))
    manager = exports.XlsxExports(tmp_path)
    manager.cleanup()
    assert not old.exists()
    assert fresh.exists() and unrelated.exists()


def test_history_export_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for frontend regression")
    result = subprocess.run([node, str(Path(__file__).with_name("test_history_export_ui.cjs"))], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rich_content", "video_frames", "long_screenshot"])
async def test_image_encoding_runs_off_event_loop(tmp_path, monkeypatch, mode):
    from auto_eval.config import VisualModeProfile, VisualExtractionConfig
    from auto_eval.judges import rich_content_judge as rich, visual_compare_judge as compare
    from auto_eval.long_screenshot import prepare_long_screenshot
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    path = tmp_path / "image.png"
    Image.new("RGB", (40, 200), "white").save(path)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    class StopAtModel(Exception):
        pass
    class Client:
        persona = "end_user"
        async def complete(self, *args, **kwargs):
            raise StopAtModel()
    module = rich if mode == "rich_content" else compare
    function = "encode_original_image" if mode == "long_screenshot" else "encode_frame"
    original = getattr(module, function)
    def encode(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, function, encode)
    if mode == "rich_content":
        judge = rich.RichContentJudge(Client(), profile)
        kwargs = {"question": "q", "context": "", "answer_text": "", "frames": [str(path)]}
    else:
        judge = compare.VisualCompareJudge(Client(), profile)
        kwargs = {"question": "q", "frames1": [str(path)], "frames2": [str(path)], "evidence_mode": mode}
        if mode == "long_screenshot":
            meta = prepare_long_screenshot(path, tmp_path / "parts", profile.long_screenshot)
            kwargs["screenshot_metas"] = [meta, meta]
    job = asyncio.create_task(judge.evaluate(**kwargs))
    try:
        await wait_until(entered.is_set)
        # A request can execute while the encoder is still blocked in its worker.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            assert (await client.get("/api/queue")).status_code == 200
        assert not job.done()
    finally:
        release.set()
    with pytest.raises(StopAtModel):
        await job
