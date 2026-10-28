import copy
import json

import httpx
import pytest

from auto_eval.judges.compare_protocols import DEFAULT_COMPARE_PROTOCOL_ID, QA_CHAT_COMPARE_PROTOCOL_ID
from auto_eval.web import history, server


@pytest.fixture
def metadata(monkeypatch):
    rows = [
        {"task_id": "recent", "dataset_name": "中文 Alpha.CSV", "evaluation_profile": DEFAULT_COMPARE_PROTOCOL_ID,
         "created_at": 400, "updated_at": 450},
        {"task_id": "other", "dataset_name": "Other.tsv", "evaluation_profile": QA_CHAT_COMPARE_PROTOCOL_ID,
         "created_at": 300, "updated_at": None},
        {"task_id": "fallback", "dataset_name": "数据 beta", "evaluation_profile": DEFAULT_COMPARE_PROTOCOL_ID,
         "created_at": 200, "updated_at": "invalid"},
        {"task_id": "archived", "dataset_name": "中文 ALPHA.csv", "evaluation_profile": QA_CHAT_COMPARE_PROTOCOL_ID,
         "created_at": 100, "updated_at": 250},
        {"task_id": "undated", "dataset_name": "", "evaluation_profile": DEFAULT_COMPARE_PROTOCOL_ID},
    ]
    monkeypatch.setattr(server, "TASKS", {})
    monkeypatch.setattr(server, "list_snapshots", lambda limit=50: copy.deepcopy(rows[:limit]))
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(("params", "expected"), [
    ({"dataset_name": "  中文 aLpHa  "}, ["recent", "archived"]),
    ({"dataset_name": "不存在"}, []),
    ({"dataset_name": "  "}, ["recent", "other", "fallback", "archived", "undated"]),
    ({"evaluation_profile": QA_CHAT_COMPARE_PROTOCOL_ID}, ["other", "archived"]),
    ({"evaluation_profile": "qa_chat_compare"}, []),
    ({"start_time": 250, "end_time": 450}, ["recent", "other", "archived"]),
    ({"start_time": 450, "end_time": 450}, ["recent"]),
    ({"start_time": 300}, ["recent", "other"]),
    ({"end_time": 250}, ["fallback", "archived"]),
    ({"start_time": 200, "end_time": 200}, ["fallback"]),
    ({"dataset_name": "alpha", "evaluation_profile": QA_CHAT_COMPARE_PROTOCOL_ID,
      "start_time": 250, "end_time": 250}, ["archived"]),
    ({"dataset_name": "alpha", "evaluation_profile": QA_CHAT_COMPARE_PROTOCOL_ID, "start_time": 251}, []),
])
async def test_history_filters_match_names_standards_and_displayed_time(metadata, params, expected):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/history", params={"page": 1, **params})
    assert response.status_code == 200
    data = response.json()
    assert [row["task_id"] for row in data["items"]] == expected
    assert (data["page"], data["page_size"], data["total"]) == (1, 10, len(expected))


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"start_time": "nan"}, {"end_time": "inf"}, {"start_time": "-inf"},
    {"start_time": "invalid"}, {"start_time": 20, "end_time": 10},
])
async def test_invalid_history_time_range_is_rejected(metadata, params):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/history", params={"page": 1, **params})
    assert response.status_code == 422


@pytest.mark.parametrize(("updated", "created", "expected"), [
    (0, 10, 10), (False, 10, 10), ("invalid", 10, 10), (float("nan"), 10, 10),
    (float("inf"), 10, 10), ("12.5", 10, 12.5), (None, None, None),
    (None, "invalid", None),
])
def test_history_display_time_uses_valid_updated_time_or_created_time(updated, created, expected):
    assert server._history_display_time({"updated_at": updated, "created_at": created}) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["invalid", float("nan"), float("inf"), False])
async def test_invalid_cached_times_use_visible_fallback_and_remain_json_safe(metadata, invalid):
    metadata[0]["updated_at"] = invalid
    metadata[-1]["created_at"] = float("nan")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/history", params={"page": 1, "start_time": 400, "end_time": 400})
        assert response.status_code == 200
        assert response.json()["items"][0]["updated_at"] is None
        assert response.json()["items"][0]["created_at"] == 400
        unfiltered = await client.get("/api/history?page=1")
    assert unfiltered.status_code == 200
    assert unfiltered.json()["items"][-1]["created_at"] is None


@pytest.mark.asyncio
async def test_filtered_history_pages_limit_after_search_and_keep_legacy_contract(metadata):
    # 250 matching archives follow 210 newer unrelated records. Searching must
    # find archives outside the unfiltered display window and cap only matches.
    rows = [
        {"task_id": f"recent-{index}", "dataset_name": "unrelated", "created_at": 1000 - index}
        for index in range(210)
    ] + [
        {"task_id": f"match-{index}", "dataset_name": "测评归档", "created_at": 500 - index}
        for index in range(250)
    ]
    metadata[:] = rows
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        unfiltered = (await client.get("/api/history?page=1")).json()
        assert unfiltered["total"] == 200
        assert unfiltered["items"][0]["task_id"] == "recent-0"
        first = (await client.get("/api/history", params={"page": 1, "dataset_name": "测评归档"})).json()
        assert (first["total"], first["page"], len(first["items"])) == (200, 1, 10)
        assert first["items"][0]["task_id"] == "match-0"
        last = (await client.get("/api/history", params={"page": 999, "dataset_name": "测评归档"})).json()
        assert (last["total"], last["page"], len(last["items"])) == (200, 20, 10)
        assert last["items"][-1]["task_id"] == "match-199"
        legacy = (await client.get("/api/history", params={"limit": 3, "dataset_name": "测评归档"})).json()
        assert set(legacy) == {"items"}
        assert [row["task_id"] for row in legacy["items"]] == ["match-0", "match-1", "match-2"]


@pytest.mark.asyncio
async def test_search_restores_legacy_standard_from_real_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(server, "TASKS", {})
    (tmp_path / "legacy.json").write_text(json.dumps({
        "task_id": "legacy", "mode": "compare", "dataset_name": "旧数据集.csv", "status": "done",
        "created_at": 1_800_000_100, "updated_at": 1_800_000_200, "items": [],
        "results": [{"index": 0, "standard_version": "0.2-simplified"}],
    }), encoding="utf-8")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/history", params={
            "page": 1, "dataset_name": "旧数据集", "evaluation_profile": DEFAULT_COMPARE_PROTOCOL_ID,
            "start_time": 1_800_000_200, "end_time": 1_800_000_200,
        })
    assert response.status_code == 200
    assert [row["task_id"] for row in response.json()["items"]] == ["legacy"]
    assert (tmp_path / "legacy.json").exists()
