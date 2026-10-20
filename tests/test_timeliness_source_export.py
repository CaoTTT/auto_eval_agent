"""Source downloads preserve both generations of the timeliness contract."""
from copy import deepcopy
import json

import httpx
import pytest

from auto_eval.web import history, server, tasks
from test_history_source_download import source_api
from test_timeliness_statistics import dataset


@pytest.mark.asyncio
async def test_timeliness_source_download_restores_compact_scores_and_frozen_protocol(source_api):
    snapshot, rows, protocol = dataset(((3, 2, None),))
    item = snapshot["items"][0]
    for product in (1, 2, 3):
        item.update({f"answer{product}": f"产品{product}", f"video{product}": f"{product}.mp4"})
    # Original file formatting and unknown input fields must survive separately
    # from the parsed items and the finalized, compact timeliness result.
    content = ('\ufeff[\r\n  {"query":"今天几点开园？", "extra": "原样保留"}\r\n]\r\n').encode("utf-8")
    expected_results = deepcopy(rows)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        uploaded = await client.post(
            "/api/upload/dataset-source", files={"file": ("时效性测评源文件.json", content)}
        )
        assert uploaded.status_code == 200, uploaded.text
        source = uploaded.json()
        created = await client.post("/api/eval", json={
            "mode": "compare",
            "evaluation_profile": protocol.id,
            "items": [item],
            "source_file_id": source["id"],
        })
        assert created.status_code == 200, created.text
        task_id = created.json()["task_id"]
        task = source_api[task_id]
        task.results = deepcopy(rows)
        assert history.save_task(task)
        source_api.clear()

        response = await client.get(f"/api/history/{task_id}")
        assert response.status_code == 200, response.text
        restored = response.json()
        assert restored["protocol_executable"]
        assert restored["evaluation_profile"] == "timeliness_compare@1.0"
        assert restored["protocol_manifest"]["bundle_revision"] == "1.0.1"
        assert restored["protocol_manifest"]["result_schema_version"] == "timeliness-result-2"
        assert restored["source_files"] == [source]
        assert restored["results"] == expected_results
        row = history.export_rows(restored)["逐题结果"][0]
        assert row["产品1时效性分"] == 3
        assert row["产品2时效性分"] == 2
        assert row["产品3时效性分"] is None
        assert row["时效性理由（维度共享）"] == expected_results[0]["timeliness_reason"]
        downloaded = await client.get(f"/api/history/{task_id}/source-files/{source['id']}")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == content


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,schema", [
    ("1.0.0", "timeliness-result-1"),
    ("1.0.1", "timeliness-result-2"),
])
async def test_unarchived_timeliness_history_remains_readable(source_api, revision, schema):
    snapshot, rows, _ = dataset(revision=revision)
    task = tasks._task_from_snapshot(snapshot, snapshot["task_id"])
    assert history.save_task(task)
    # History written before source archiving has no source_files field at all.
    path = history._find_task_path(task.id)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.pop("source_files")
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get(f"/api/history/{task.id}")
        assert response.status_code == 200, response.text
        restored = response.json()
        assert restored["protocol_executable"]
        assert restored["protocol_manifest"]["bundle_revision"] == revision
        assert restored["protocol_manifest"]["result_schema_version"] == schema
        assert restored["results"] == rows
        assert restored["source_files"] == []
        exported = history.export_rows(restored)["逐题结果"][0]
        assert exported["产品1时效性分"] == 3
        assert exported["时效性理由（维度共享）"] == rows[0]["timeliness_reason"]
        missing = await client.get(f"/api/history/{task.id}/source-files/" + "a" * 32)
        assert missing.status_code == 404
        assert missing.json()["detail"] == "未保存该源文件"
