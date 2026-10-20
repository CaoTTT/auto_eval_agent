"""Exercise the platform download endpoints, not only workbook helpers."""
import asyncio
from collections import OrderedDict
from io import BytesIO
import json

import httpx
from openpyxl import load_workbook
import pytest

from auto_eval.web import exports, history, server, tasks
from test_timeliness_statistics import dataset


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ("1.0.0", "1.0.1", "1.0.2"))
@pytest.mark.parametrize("restored", (False, True), ids=("memory", "history"))
@pytest.mark.parametrize("count", (2, 3))
async def test_platform_xlsx_download_keeps_frozen_timeliness_dimensions(
    tmp_path, monkeypatch, revision, restored, count,
):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    registry = OrderedDict()
    monkeypatch.setattr(tasks, "TASKS", registry)
    monkeypatch.setattr(server, "TASKS", registry)
    manager = exports.XlsxExports(tmp_path / "exports")
    monkeypatch.setattr(server, "XLSX_EXPORTS", manager)
    snapshot, results, protocol = dataset(((3,) * count,), revision)
    snapshot.update(status="done", dataset_name="时效性下载回归.jsonl")
    task = tasks._task_from_snapshot(snapshot, snapshot["task_id"])
    if restored:
        assert history.save_task(task)
    else:
        registry[task.id] = task

    def assert_workbook(content):
        workbook = load_workbook(BytesIO(content), read_only=True)
        try:
            headers, values = list(workbook["逐题结果"].values)[:2]
            row = dict(zip(headers, values))
            for product in range(1, count + 1):
                for dimension in ("理解需求", "权威性", "时效性"):
                    assert row[f"产品{product}{dimension}分"] == 3
            assert row["协议包修订"] == revision
            assert row["结果Schema版本"] == protocol.result_schema_version
            assert not any("准确性" in key or "需求闭环" in key or "排名" in key for key in headers)
            assert ("内容冲突" in headers) is (revision == "1.0.2")
            if revision == "1.0.2":
                assert row["内容冲突理由"] == results[0]["conflict_reason"]
                assert row["内容冲突"] == "无冲突"
            info_headers, info_values = list(workbook["运行信息"].values)[:2]
            info = dict(zip(info_headers, info_values))
            assert info["evaluation_profile"] == protocol.id
            assert json.loads(info["protocol_manifest"])["bundle_revision"] == revision
            # A stored timeliness summary can coexist with a legacy details
            # template. Check the generated statistics sheet independently.
            statistics = list(workbook["对比评估统计"].values)
            labels = {row[0] for row in statistics if row}
            assert "时效性评估统计" in labels
            assert "评分统计不可用" not in labels
            for dimension in ("理解需求", "权威性", "时效性"):
                for product in range(1, count + 1):
                    assert any(
                        row[:2] == (dimension, f"产品{product}")
                        and row[3] == 1 and row[6] == 3
                        for row in statistics
                    )
        finally:
            workbook.close()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test",
        ) as client:
            # The button uses POST -> status polling -> GET download.
            response = await client.post(f"/api/eval/{task.id}/exports?include_images=false")
            assert response.status_code == 202, response.text
            export_id = response.json()["export_id"]
            await asyncio.wait_for(manager.jobs[export_id]["done"].wait(), timeout=10)
            state = await client.get(f"/api/exports/{export_id}")
            assert state.json()["status"] == "ready", state.text
            downloaded = await client.get(f"/api/exports/{export_id}/download")
            assert downloaded.status_code == 200, downloaded.text
            assert_workbook(downloaded.content)
            # The older direct download link must use the identical protocol.
            direct = await client.get(f"/api/eval/{task.id}/export?format=xlsx&include_images=false")
            assert direct.status_code == 200, direct.text
            assert_workbook(direct.content)
    finally:
        await manager.close()
