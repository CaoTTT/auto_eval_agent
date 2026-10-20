"""Source downloads must preserve the cached history listing used before upgrade."""
import json
from pathlib import Path

import httpx
import pytest

from auto_eval.web import history, server


SOURCE = {
    "id": "a" * 32, "filename": "original.csv", "size": 12, "sha256": "b" * 64,
}


def cached_record(directory, task_id, *, version=2, mode="compare", sources=False, created_at=1):
    snapshot = {
        "task_id": task_id, "mode": mode, "dataset_name": f"{task_id}.csv",
        "items": [{"id": "one", "query": "Original input"}],
        "results": [{"index": 0, "answer": "Original result"}],
        "status": "done", "created_at": 1_800_000_000 + created_at,
    }
    if sources:
        snapshot["source_files"] = [SOURCE]
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    row = history._snapshot_meta_row(snapshot, path)
    row["meta_version"] = version
    if not sources:
        row.pop("source_files", None)
    metadata = history._meta_path(path)
    metadata.write_text(json.dumps(row), encoding="utf-8")
    return path, metadata


def file_state(paths):
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def prohibit_snapshot_reads(monkeypatch, snapshots):
    original_read = Path.read_text
    reads = []
    snapshot_paths = set(snapshots)

    def read_text(path, *args, **kwargs):
        if path in snapshot_paths:
            reads.append(path)
            raise AssertionError("History listing must use its existing metadata cache")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    return reads


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("sources", [False, True])
def test_existing_metadata_lists_without_reading_or_rewriting_history(tmp_path, monkeypatch, version, sources):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    snapshot, metadata = cached_record(tmp_path, "old-task", version=version, sources=sources)
    before = file_state([snapshot, metadata])
    reads = prohibit_snapshot_reads(monkeypatch, [snapshot])

    rows = history.list_snapshots(limit=None)

    assert len(rows) == 1
    assert rows[0]["task_id"] == "old-task"
    assert rows[0]["done"] == rows[0]["total"] == 1
    assert rows[0]["source_files"] == ([SOURCE] if sources else [])
    assert "meta_version" not in rows[0]
    assert reads == []
    assert file_state([snapshot, metadata]) == before


@pytest.mark.asyncio
async def test_history_and_dataset_pages_keep_all_legacy_cache_records(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(server, "TASKS", {})
    records = [
        cached_record(tmp_path, f"legacy-{index}", mode="rich_content" if index % 3 == 0 else "compare",
                      created_at=index + 1)
        for index in range(120)
    ]
    records.append(cached_record(tmp_path, "source-task", version=3, sources=True, created_at=121))
    before = file_state([path for record in records for path in record])
    reads = prohibit_snapshot_reads(monkeypatch, [snapshot for snapshot, _ in records])

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get("/api/history", params={"page": 1})
        assert response.status_code == 200
        page = response.json()
        assert (page["total"], page["page_size"]) == (121, 10)
        assert page["items"][0]["source_files"] == [SOURCE]
        assert page["items"][1]["source_files"] == []
        last_history = (await client.get("/api/history", params={"page": 13})).json()
        assert (last_history["total"], last_history["items"][0]["task_id"]) == (121, "legacy-0")

        dataset_ids = []
        for number in range(1, 10):
            response = await client.get("/api/datasets", params={"page": number})
            assert response.status_code == 200
            page = response.json()
            assert page["total"] == 81
            dataset_ids.extend(row["task_id"] for row in page["items"])
        assert dataset_ids == ["source-task", *[f"legacy-{index}" for index in reversed(range(120)) if index % 3]]

    assert reads == []
    assert file_state([path for record in records for path in record]) == before
