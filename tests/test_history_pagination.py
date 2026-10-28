import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from auto_eval.web import history, server


@pytest.mark.asyncio
async def test_history_pages_reach_all_records_and_adjust_after_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(server, "TASKS", {})
    for index in range(61):
        (tmp_path / f"record-{index}.json").write_text(json.dumps({
            "task_id": f"record-{index}", "mode": "compare", "status": "done",
            "created_at": 1_800_000_000 + index, "items": [], "results": [],
        }), encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        seen = []
        for page in range(1, 8):
            response = await client.get("/api/history", params={"page": page})
            assert response.status_code == 200
            data = response.json()
            assert (data["page"], data["page_size"], data["total"]) == (page, 10, 61)
            assert len(data["items"]) == (10 if page < 7 else 1)
            seen.extend(row["task_id"] for row in data["items"])
        assert seen == [f"record-{index}" for index in reversed(range(61))]

        assert (await client.delete("/api/history/record-0")).status_code == 200
        data = (await client.get("/api/history?page=7")).json()
        assert (data["page"], data["total"], len(data["items"])) == (6, 60, 10)
        assert data["items"][-1]["task_id"] == "record-1"
        assert (await client.get("/api/history?page=0")).json()["page"] == 1
        assert (await client.get("/api/history?page=999")).json()["page"] == 6
        assert (await client.get("/api/history?page=invalid")).status_code == 422

        # Existing callers using limit keep the original response contract.
        legacy = (await client.get("/api/history?limit=3")).json()
        assert set(legacy) == {"items"}
        assert len(legacy["items"]) == 3
        assert len((await client.get("/api/history")).json()["items"]) == 50


@pytest.mark.asyncio
async def test_empty_history_returns_first_page(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    assert await server.api_history(page=9) == {"items": [], "total": 0, "page": 1, "page_size": 10}


@pytest.mark.asyncio
async def test_history_panel_only_pages_latest_200_without_removing_older_records(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(server, "TASKS", {})
    for index in range(211):
        (tmp_path / f"record-{index}.json").write_text(json.dumps({
            "task_id": f"record-{index}", "mode": "compare", "status": "done",
            "created_at": 1_800_000_000 + index, "items": [], "results": [],
        }), encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        seen = []
        for page in range(1, 21):
            data = (await client.get("/api/history", params={"page": page})).json()
            assert (data["page"], data["page_size"], data["total"]) == (page, 10, 200)
            assert len(data["items"]) == 10
            seen.extend(row["task_id"] for row in data["items"])
        assert seen == [f"record-{index}" for index in reversed(range(11, 211))]
        overflow = (await client.get("/api/history?page=999")).json()
        assert (overflow["page"], overflow["total"]) == (20, 200)
        assert overflow["items"][-1]["task_id"] == "record-11"

        # Hiding old rows must not delete snapshots or restrict direct access.
        oldest = await client.get("/api/history/record-0")
        assert oldest.status_code == 200
        assert oldest.json()["task_id"] == "record-0"
        assert (tmp_path / "record-0.json").exists()
        assert len(history.list_snapshots(limit=None)) == 211
        legacy = (await client.get("/api/history?limit=500")).json()
        assert len(legacy["items"]) == 211

        # Removing a visible row pulls the next archived row into the window.
        assert (await client.delete("/api/history/record-210")).status_code == 200
        last = (await client.get("/api/history?page=20")).json()
        assert (last["page"], last["total"], len(last["items"])) == (20, 200, 10)
        assert last["items"][-1]["task_id"] == "record-10"
        assert (tmp_path / "record-0.json").exists()


def test_history_pagination_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("test_history_pagination_ui.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
