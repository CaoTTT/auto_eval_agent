import copy
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from auto_eval import query_images
from auto_eval.config import QueryImageConfig
from auto_eval.web import history, server
from auto_eval.web.dataset_media import DatasetMedia
from auto_eval.web.tasks import Task


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(history, "HISTORY_DIR", root / "history")
    monkeypatch.setattr(server, "BASE_DIR", root)
    monkeypatch.setattr(query_images, "PROJECT_ROOT", root)
    monkeypatch.setattr(query_images, "RUNS_DIR", root / "runs")
    monkeypatch.setattr(query_images, "resolve_project_path", lambda path: root / path)
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", "")
    monkeypatch.setattr(server, "DATASET_MEDIA", DatasetMedia(capacity=4))
    monkeypatch.setitem(server._state, "cfg", SimpleNamespace(visual_modes={
        "rich_content": SimpleNamespace(query_images=QueryImageConfig())
    }))
    return root


@pytest.mark.asyncio
async def test_dataset_list_filters_before_pagination_and_keeps_duplicate_names(environment):
    for index in range(25):
        task = Task(id=f"dataset-{index}", mode="compare", items=[{"query":str(index)}], options={},
                    dataset_name="同名数据.jsonl", status="done", created_at=1700000000+index)
        assert history.save_task(task)
    assert history.save_task(Task(id="rich",mode="rich_content",items=[{"query":"rich"}],options={},created_at=1700000050))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        pages=[(await client.get(f"/api/datasets?page={page}")).json() for page in (1,2,3)]
        assert [len(page["items"]) for page in pages] == [10,10,5]
        assert all(page["total"] == 25 for page in pages)
        ids=[row["task_id"] for page in pages for row in page["items"]]
        assert len(set(ids))==25 and ids[0]=="dataset-24"
        assert all("items" not in row and "results" not in row for page in pages for row in page["items"])
        found=(await client.get("/api/datasets",params={"search":"dataset-13"})).json()
        assert found["total"]==1 and found["items"][0]["task_id"]=="dataset-13"


@pytest.mark.asyncio
async def test_preview_returns_inputs_only_without_mutating_original(environment,monkeypatch):
    items=[{"id":str(i),"query":"q","video1":"a.mp4","video2":"b.mp4",
            "query_images":["original.png"],"query_image_meta":[{"original_path":"original.png"}],
            "source_data":{"custom":"preserved"},"frames1":["cache.jpg"]} for i in range(25)]
    task=Task(id="source",mode="compare",items=items,options={},results=[{"index":0,"score":3}])
    before=copy.deepcopy(task.items)
    async def peek(key):return task if key==task.id else None
    monkeypatch.setattr(server,"peek_task_async",peek)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        data=(await client.get("/api/datasets/source")).json()
        assert len(data["items"])==25 and "results" not in data
        assert "frames1" not in data["items"][0]
        assert data["items"][0]["query_image_meta"]==items[0]["query_image_meta"]
        assert data["items"][0]["source_data"]=={"custom":"preserved"}
        data["items"][0]["query"]="changed"
        assert task.items==before
        assert (await client.get("/api/datasets/missing")).status_code==404
        task.mode="rich_content"
        assert (await client.get("/api/datasets/source")).status_code==422


@pytest.mark.asyncio
@pytest.mark.parametrize("role",["query","screenshot","video"])
async def test_media_preview_checks_authorization_original_bytes_and_changes(environment,role):
    path=environment/("clip.mp4" if role=="video" else "original.png")
    if role=="video":path.write_bytes(b"test video")
    else:Image.new("RGB",(80,100),(20,50,90)).save(path)
    original=path.read_bytes()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        response=await client.post("/api/dataset-media",json={"role":role,"path":str(path),"expected_sha256":hashlib.sha256(original).hexdigest()})
        assert response.status_code==200,response.text
        urls=response.json()
        assert (await client.get(urls["preview_url"])).content==original
        downloaded=await client.get(urls["download_url"])
        assert downloaded.content==original and "attachment" in downloaded.headers["content-disposition"]
        assert (await client.post("/api/dataset-media",json={"role":role,"path":str(environment.parent/path.name)})).status_code==422
        assert (await client.post("/api/dataset-media",json={"role":role,"path":"https://example.test/file.png"})).status_code==422
        assert (await client.get("/api/dataset-media/unknown")).status_code==404
        path.write_bytes(b"changed")
        assert (await client.get(urls["preview_url"])).status_code==404


@pytest.mark.asyncio
async def test_file_check_reports_case_and_role_without_calling_model(environment):
    Image.new("RGB",(20,20)).save(environment/"q.png")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),base_url="http://test") as client:
        data=(await client.post("/api/dataset-files/validate",json={"files":[
            {"index":0,"role":"query","path":"q.png","label":"提问图片"},
            {"index":12,"role":"video","path":"missing.mp4","label":"产品2录屏"}
        ]})).json()
        assert data["checked"]==2 and len(data["issues"])==1
        assert data["issues"][0]["index"]==12 and data["issues"][0]["label"]=="产品2录屏"
