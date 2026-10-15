import asyncio
import threading

import httpx
import pytest
from fastapi.responses import Response

from auto_eval.web import exports, server
from auto_eval.web.tasks import Task


async def settle(manager):
    await asyncio.wait_for(asyncio.gather(*list(manager.workers)), 5)


async def wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(poll(), 3)


@pytest.mark.asyncio
async def test_direct_download_cannot_bypass_full_export_queue(tmp_path, monkeypatch):
    manager = exports.XlsxExports(tmp_path, capacity=1)
    monkeypatch.setattr(server, "XLSX_EXPORTS", manager)
    async def peek(key):
        return Task(id=key, mode="compare", items=[], options={})
    monkeypatch.setattr(server, "peek_task_async", peek)
    monkeypatch.setattr(exports, "peek_task_async", peek)
    monkeypatch.setattr(server, "_export_snapshot", lambda *a, **kw: Response(b"bypassed queue"))
    monkeypatch.setattr(manager, "_write", lambda snapshot, path, **kw: path.write_bytes(b"test"))
    await manager.slot.acquire()
    try:
        manager.create("a")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
            response = await asyncio.wait_for(client.get("/api/eval/b/export?format=xlsx"), 2)
            assert response.status_code == 429
            assert response.headers["Retry-After"] == "3"
    finally:
        manager.slot.release()
        await settle(manager)
        await manager.close()


@pytest.mark.asyncio
async def test_many_clients_share_bounded_queue_without_blocking_other_apis(tmp_path, monkeypatch):
    manager = exports.XlsxExports(tmp_path)
    monkeypatch.setattr(server, "XLSX_EXPORTS", manager)
    async def peek(key):
        return Task(id=key, mode="compare", items=[], options={})
    monkeypatch.setattr(exports, "peek_task_async", peek)
    entered, release = threading.Event(), threading.Event()
    writes = []
    def write(snapshot, path, **kwargs):
        writes.append(snapshot["task_id"])
        if snapshot["task_id"] == "first":
            entered.set()
            assert release.wait(10)
        path.write_text(snapshot["task_id"], encoding="utf-8")
    monkeypatch.setattr(manager, "_write", write)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        try:
            first = (await client.post("/api/eval/first/exports")).json()
            await wait_until(entered.is_set)
            responses = await asyncio.wait_for(asyncio.gather(*[
                client.post(f"/api/eval/user-{i}/exports") for i in range(11)
            ]), 3)
            accepted = [response.json() for response in responses if response.status_code == 202]
            assert len(accepted) == 3
            assert sum(response.status_code == 429 for response in responses) == 8
            assert sorted(job["queue_position"] for job in accepted) == [1, 2, 3]
            duplicates = await asyncio.wait_for(asyncio.gather(*[
                client.post("/api/eval/first/exports") for _ in range(20)
            ]), 3)
            assert all(response.status_code == 202 and response.json()["export_id"] == first["export_id"]
                       for response in duplicates)
            assert (await asyncio.wait_for(client.get("/api/queue"), 2)).status_code == 200
            assert writes == ["first"], "only the first writer may occupy a thread"
            assert len(manager.workers) == 4
            release.set()
            await settle(manager)
            states = [first, *accepted]
            downloads = await asyncio.gather(*[
                client.get(f"/api/exports/{state['export_id']}/download") for state in states
            ])
            assert [response.status_code for response in downloads] == [200] * 4
            assert [response.text for response in downloads] == [state["task_id"] for state in states]
            assert all(job["downloads"] == 0 for job in manager.jobs.values())
        finally:
            release.set()
            await manager.close()


@pytest.mark.asyncio
async def test_cancelled_direct_client_does_not_cancel_shared_generation(tmp_path, monkeypatch):
    manager = exports.XlsxExports(tmp_path)
    monkeypatch.setattr(server, "XLSX_EXPORTS", manager)
    async def peek(key):
        return Task(id=key, mode="compare", items=[], options={})
    monkeypatch.setattr(exports, "peek_task_async", peek)
    monkeypatch.setattr(manager, "_write", lambda snapshot, path, **kw: path.write_bytes(b"shared file"))
    await manager.slot.acquire()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        direct = asyncio.create_task(client.get("/api/eval/shared/export?format=xlsx"))
        try:
            await wait_until(lambda: bool(manager.jobs))
            state = (await client.post("/api/eval/shared/exports")).json()
            key = state["export_id"]
            assert len(manager.jobs) == 1 and manager.jobs[key]["waiters"] == 1
            direct.cancel()
            with pytest.raises(asyncio.CancelledError):
                await direct
            assert manager.jobs[key]["waiters"] == 0
            assert len(manager.workers) == 1
        finally:
            direct.cancel()
            await asyncio.gather(direct, return_exceptions=True)
            manager.slot.release()
        try:
            await settle(manager)
            download = await client.get(f"/api/exports/{key}/download")
            assert download.status_code == 200 and download.content == b"shared file"
        finally:
            await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "send_error"])
async def test_interrupted_download_releases_file_lease(tmp_path, monkeypatch, failure):
    manager = exports.XlsxExports(tmp_path)
    async def peek(key):
        return Task(id=key, mode="compare", items=[], options={})
    monkeypatch.setattr(exports, "peek_task_async", peek)
    monkeypatch.setattr(manager, "_write", lambda snapshot, path, **kw: path.write_bytes(b"content"))
    key = manager.create("a")["export_id"]
    await settle(manager)
    response = manager.download(key)
    path = manager.jobs[key]["path"]
    sending = asyncio.Event()
    async def send(message):
        if message["type"] == "http.response.body":
            sending.set()
            if failure == "send_error":
                raise OSError("client disconnected")
            await asyncio.Event().wait()
    async def receive():
        return {"type": "http.disconnect"}
    transfer = asyncio.create_task(response({"type": "http", "method": "GET", "headers": []}, receive, send))
    try:
        await asyncio.wait_for(sending.wait(), 3)
        if failure == "cancel":
            transfer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await transfer
        else:
            with pytest.raises(OSError, match="client disconnected"):
                await transfer
        assert manager.jobs[key]["downloads"] == 0
        manager.jobs[key]["finished"] -= manager.ttl + 1
        manager.cleanup()
        assert not path.exists() and key not in manager.jobs
    finally:
        transfer.cancel()
        await asyncio.gather(transfer, return_exceptions=True)
        await manager.close()


@pytest.mark.asyncio
async def test_failed_export_releases_slot_for_next_waiting_client(tmp_path, monkeypatch):
    manager = exports.XlsxExports(tmp_path)
    async def peek(key):
        return Task(id=key, mode="compare", items=[], options={})
    monkeypatch.setattr(exports, "peek_task_async", peek)
    def write(snapshot, path, **kwargs):
        path.write_bytes(b"partial" if snapshot["task_id"] == "broken" else b"complete")
        if snapshot["task_id"] == "broken":
            raise OSError("disk full")
    monkeypatch.setattr(manager, "_write", write)
    try:
        broken = manager.create("broken")["export_id"]
        healthy = manager.create("healthy")["export_id"]
        await settle(manager)
        assert manager.view(broken)["status"] == "error"
        assert not manager.jobs[broken]["path"].exists()
        assert manager.view(healthy)["status"] == "ready"
        assert manager.jobs[healthy]["path"].read_bytes() == b"complete"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_inflight_download_survives_expiration_and_count_cleanup(tmp_path, monkeypatch):
    manager = exports.XlsxExports(tmp_path)
    monkeypatch.setattr(server, "XLSX_EXPORTS", manager)
    async def peek(key):
        return Task(id=key, mode="compare", items=[], options={})
    monkeypatch.setattr(exports, "peek_task_async", peek)
    monkeypatch.setattr(manager, "_write", lambda snapshot, path, **kw: path.write_bytes(b"file content"))
    key = manager.create("a")["export_id"]
    await settle(manager)
    response1 = await server.api_xlsx_download(key)
    response2 = await server.api_xlsx_download(key)
    try:
        for i in range(18):
            manager.create(f"next-{i}")
            await settle(manager)
        manager.jobs[key]["finished"] -= manager.ttl + 1
        manager.cleanup()
        assert manager.jobs[key]["path"].is_file()
        await response1.background()
        manager.cleanup()
        assert key in manager.jobs, "the other downloader still needs the file"
        await response2.background()
        manager.cleanup()
        assert key not in manager.jobs
    finally:
        if response1.background:
            await response1.background()
        if response2.background:
            await response2.background()
        await manager.close()
