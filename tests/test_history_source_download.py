"""Original dataset bytes survive evaluation, restore, reuse and history deletion."""
from collections import OrderedDict
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.web import history, server, tasks


@pytest.fixture
def source_api(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    registry = OrderedDict()
    monkeypatch.setattr(tasks, "TASKS", registry)
    monkeypatch.setattr(server, "TASKS", registry)
    app_cfg = AppConfig(
        judges=[JudgeConfig(name="judge_2", model="fake")],
        visual_modes={"rich_content": VisualModeProfile(extraction={"algorithm_version": "test"})},
    )
    monkeypatch.setattr(server, "cfg", lambda: app_cfg)

    def save(task, **kwargs):
        assert history.save_task(task)

    def enqueue(task, *args):
        task.status = "done"
        save(task)
        return 1

    monkeypatch.setattr(tasks, "queue_task_save", save)
    monkeypatch.setattr(server, "queue_task_save", save)
    monkeypatch.setattr(server, "EVAL_SCHEDULER", SimpleNamespace(enqueue=enqueue))
    monkeypatch.setattr(server, "spawn_background", lambda coroutine: coroutine.close())
    return registry


def evaluation(mode="rich_content", **extra):
    item = {"id": "one", "query": "修改后的评测输入"}
    if mode == "compare":
        item.update(answer1="A", answer2="B", video1="a.mp4", video2="b.mp4",
                    context1="产品1背景", context2="产品2背景")
    return {"mode": mode, "items": [item], **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rich_content", "compare"])
@pytest.mark.parametrize("filename,content", [
    ("中文 原始数据.JSONL", b'\xef\xbb\xbf{ "id": "one", "custom": true }\r\n\r\n'),
    ("源数据.CSV", 'id,query,extra\r\none,"原始,问题",保留\r\n'.encode("utf-8-sig")),
    ("原文件.json", b'[\n  {"id":"one", "unknown_field": 42}\n]\n'),
])
async def test_upload_eval_history_and_restart_download_exact_original(source_api, mode, filename, content):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        uploaded = await client.post("/api/upload/dataset-source", files={"file": (filename, content)})
        assert uploaded.status_code == 200, uploaded.text
        source = uploaded.json()
        assert source["filename"] == filename
        assert source["size"] == len(content)
        assert source["sha256"] == hashlib.sha256(content).hexdigest()
        created = await client.post("/api/eval", json=evaluation(mode, source_file_id=source["id"]))
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        # Force all subsequent reads through the persisted history, as after restart.
        source_api.clear()
        detail = (await client.get(f"/api/history/{task_id}")).json()
        assert detail["source_files"] == [source]
        assert detail["items"][0]["query"] == "修改后的评测输入"
        rows = (await client.get("/api/history", params={"page": 1})).json()["items"]
        assert rows[0]["source_files"] == [source]
        if mode == "compare":
            dataset = (await client.get(f"/api/datasets/{task_id}")).json()
            assert dataset["source_files"] == [source]
            assert (await client.get("/api/datasets")).json()["items"][0]["source_files"] == [source]
        downloaded = await client.get(f"/api/history/{task_id}/source-files/{source['id']}")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == content
        assert downloaded.headers["content-disposition"] == f"attachment; filename*=utf-8''{quote(filename)}"
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["source_task_id", "legacy_compare_option"])
async def test_reused_source_survives_original_history_deletion(source_api, reference):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        content = b'original bytes,\r\nwith spacing  \r\n'
        source = (await client.post("/api/upload/dataset-source", files={"file": ("original.csv", content)})).json()
        original = await client.post("/api/eval", json=evaluation(source_file_id=source["id"]))
        original_id = original.json()["task_id"]
        extra = ({"source_task_id": original_id} if reference == "source_task_id" else
                 {"options": {"dataset_source_task_id": original_id}})
        reused = await client.post("/api/eval", json=evaluation(**extra))
        assert reused.status_code == 200, reused.text
        reused_id = reused.json()["task_id"]
        assert (await client.delete(f"/api/history/{original_id}")).status_code == 200
        assert (await client.get(f"/api/history/{original_id}/source-files/{source['id']}")).status_code == 404
        source_api.clear()
        downloaded = await client.get(f"/api/history/{reused_id}/source-files/{source['id']}")
        assert downloaded.status_code == 200 and downloaded.content == content


@pytest.mark.asyncio
async def test_incremental_updates_retain_all_uploaded_sources(source_api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        originals = []
        for name, content in [("first.json", b'first\r\n'), ("second.csv", b'second\r\n')]:
            originals.append(((await client.post("/api/upload/dataset-source", files={"file": (name, content)})).json(), content))
        source, _ = originals[0]
        request = evaluation(task_id="incremental", source_file_id=source["id"])
        created = await client.post("/api/eval/items", json=request)
        assert created.status_code == 200, created.text
        source_api["incremental"].active_runs = 0
        source_api["incremental"].status = "done"
        source_api["incremental"].execution_control = {}
        source, _ = originals[1]
        updated = await client.post("/api/eval/items", json=evaluation(task_id="incremental", source_file_id=source["id"]))
        assert updated.status_code == 200, updated.text
        assert len(source_api["incremental"].source_files) == 2
        source_api.clear()
        for source, content in originals:
            downloaded = await client.get(f"/api/history/incremental/source-files/{source['id']}")
            assert downloaded.status_code == 200 and downloaded.content == content


@pytest.mark.asyncio
async def test_legacy_and_manual_history_do_not_fabricate_originals(source_api):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        task = tasks.Task(id="legacy", mode="rich_content", items=[{"query": "q"}], options={},
                          dataset_name="C:/private/source.json", status="done")
        assert history.save_task(task)
        snapshot_path = history._find_task_path(task.id)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot.pop("source_files")
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        detail = (await client.get("/api/history/legacy")).json()
        assert detail["source_files"] == []
        assert (await client.get("/api/history/legacy/source-files/" + "a" * 32)).status_code == 404
        reused = await client.post("/api/eval", json=evaluation(source_task_id="legacy"))
        assert reused.status_code == 200
        assert source_api[reused.json()["task_id"]].source_files == []
        for extra, status in [({"source_file_id": "../secret"}, 422),
                              ({"source_file_id": "a" * 32}, 422),
                              ({"source_task_id": "missing"}, 404)]:
            response = await client.post("/api/eval", json=evaluation(**extra))
            assert response.status_code == status, response.text


@pytest.mark.asyncio
async def test_download_rejects_cross_task_files_and_changed_archive(source_api):
    from auto_eval.web.source_files import source_file_path

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        source = (await client.post("/api/upload/dataset-source", files={"file": ("data.csv", b'raw\r\n')})).json()
        task_id = (await client.post("/api/eval", json=evaluation(source_file_id=source["id"]))).json()["task_id"]
        other = (await client.post("/api/eval", json=evaluation())).json()["task_id"]
        assert (await client.get(f"/api/history/{other}/source-files/{source['id']}")).status_code == 404
        source_file_path(task_id, source["id"]).write_bytes(b'bad\r\n')
        assert (await client.get(f"/api/history/{task_id}/source-files/{source['id']}")).status_code == 404
