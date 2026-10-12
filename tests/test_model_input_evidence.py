import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import zipfile

import httpx
import pytest
from PIL import Image, ImageDraw

from auto_eval import evidence_audit, query_images
from auto_eval.config import AppConfig, JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.web import history, runner, server, video_prepare, screenshot_evidence
from auto_eval.web.model_input_evidence import EvidenceUnavailable, successful_records, write_model_input_zip
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.tasks import Task, _task_from_snapshot
from test_vqa_input import Client
from test_multi_turn_compare import data


def sent_images(call):
    kwargs = call[2]
    urls = kwargs.get("user_images") or [part["image_url"]["url"]
        for part in kwargs.get("content_parts", []) if part["type"] == "image_url"]
    return [base64.b64decode(url.split(",", 1)[1]) for url in urls]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    monkeypatch.setattr(evidence_audit, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(query_images, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(screenshot_evidence, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(screenshot_evidence, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "_persist_task", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **k: None)
    profile = VisualModeProfile(extraction=VisualExtractionConfig(
        algorithm_version="test", max_edge=48, jpeg_quality=40))
    profile.long_screenshot.max_pixels = 4096
    profile.query_images.allowed_roots = [str(tmp_path)]
    shots = []
    for n in (1, 2, 3):
        path = tmp_path / f"source{n}.png"
        image = Image.new("RGB", (64, 192), "white")
        draw = ImageDraw.Draw(image)
        for y in range(8, 184, 24):
            draw.rectangle((8, y, 55, y + 7), fill=(30 * n, 60, 90))
        image.save(path)
        shots.append(str(path))
        (tmp_path / f"source{n}.mp4").write_bytes(f"mock video {n}".encode())

    def extract(path, directory, **kwargs):
        frame = directory / "kf_001.jpg"
        with Image.open(shots[0]) as image:
            image.save(frame, quality=95)
        return [frame]
    prepare_video = video_prepare.prepare_session_visual_compare_item
    prepare_shot = video_prepare.prepare_session_long_screenshot_item
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", lambda item, **kwargs:
        prepare_video(item, **kwargs, base_dir=tmp_path, runs_dir=tmp_path / "runs",
                      probe_fn=lambda path: 2.0, extract_fn=extract))
    monkeypatch.setattr(runner, "prepare_session_long_screenshot_item", lambda item, **kwargs:
        prepare_shot(item, **kwargs, base_dir=tmp_path, runs_dir=tmp_path / "runs"))
    from auto_eval.judges import visual_compare_judge
    monkeypatch.setattr(visual_compare_judge, "resolve_project_path", lambda path: tmp_path / path)
    monkeypatch.setattr(history, "PROJECT_ROOT", tmp_path)
    return tmp_path, profile, shots


async def mixed_task(environment, monkeypatch, count=2):
    root, profile, shots = environment
    videos = {f"video{n}": str(root / f"source{n}.mp4") for n in range(1, count + 1)}
    screenshots = {f"screenshot{n}": shots[n - 1] for n in range(1, count + 1)}
    objects = [
        {"id": "video", "query": "video-case", "product_count": count, **videos},
        {"id": "shots", "query": "shot-case", "product_count": count, **videos, **screenshots},
        {"id": "query-video", "query": "query-case", "product_count": count,
         "query_images": [shots[0]], "screenshot1": shots[0], **videos},
        {"id": "failed", "query": "provider-fails", "product_count": count, **videos},
    ]
    items, errors = parse_jsonl(json.dumps(objects), "compare")
    assert not errors
    client = Client(count=count)
    complete = client.complete
    async def fail_one(system, user, **kwargs):
        if "provider-fails" in user:
            raise RuntimeError("provider unavailable")
        return await complete(system, user, **kwargs)
    client.complete = fail_one
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    task = Task(id="model-input-task", mode="compare", items=items, options={},
                dataset_name="C:\\datasets\\混合测试集.json", session_name="mixed_model-input-task")
    cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile})
    one, _ = runner._make_item_evaluator(task, cfg)
    for index, item in enumerate(items):
        await one(index, item)
    assert task.results[-1].get("error")
    assert all(not r.get("error") for r in task.results[:-1]), task.results
    return task, client


@pytest.mark.parametrize("count", [2, 3])
async def test_mixed_task_zip_is_exact_model_input_and_skips_failures(environment, monkeypatch, count):
    root, _, _ = environment
    task, client = await mixed_task(environment, monkeypatch, count)
    # Historical exports must not depend on the original data or frame files.
    snapshot = json.loads(json.dumps(history.task_to_snapshot(task)))
    restored = _task_from_snapshot(snapshot, task.id)
    for item in task.items:
        for n in range(1, count + 1):
            for path in item.get(f"frames{n}", []):
                (root / path).write_bytes(b"source changed")
    archive_path = write_model_input_zip(history.task_to_snapshot(restored), root / "actual.zip")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["total_cases"] == 4 and manifest["exported_cases"] == 3
        assert manifest["skipped_cases"][0]["reason"] == "evaluation_failed"
        assert not any(name.startswith("case_0004/") for name in archive.namelist())
        for record, call in zip(manifest["records"], client.calls, strict=True):
            actual = [archive.read(image["archive_path"]) for image in record["images"]]
            assert actual == sent_images(call)
        assert manifest["records"][0]["evidence_mode"] == "video_frames"
        assert manifest["records"][1]["evidence_mode"] == "long_screenshot"
        assert manifest["records"][2]["images"][0]["image_role"] == "query_image"
        assert len(manifest["records"][1]["images"]) > count  # actual long screenshot slices
        assert not any("original" in name or name.endswith(".mp4") for name in archive.namelist())
        frame = sent_images(client.calls[0])[0]
        with Image.open(BytesIO(frame)) as image:
            assert max(image.size) == 48 and image.format == "JPEG"
    assert "base64," not in json.dumps(snapshot)


async def test_multi_turn_zip_keeps_all_sent_history_and_current_images(data, environment, monkeypatch):
    items, profile, judge = data
    parsed, errors = parse_jsonl(json.dumps(items), "compare")
    assert not errors
    client = Client()
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    task = Task(id="multi-images", mode="compare", items=parsed, options={}, session_name="multi")
    await runner._run(task, AppConfig(judges=[judge], visual_modes={"rich_content": profile}))
    assert all(not r.get("error") for r in task.results), task.results
    target = write_model_input_zip(history.task_to_snapshot(task), environment[0] / "multi.zip")
    with zipfile.ZipFile(target) as archive:
        records = json.loads(archive.read("manifest.json"))["records"]
        assert len(records) == 3
        for record, call in zip(records, client.calls, strict=True):
            assert [archive.read(image["archive_path"]) for image in record["images"]] == sent_images(call)
        assert {image["source_turn"] for image in records[2]["images"]} == {1, 2, 3}
        assert {image["request_role"] for image in records[2]["images"]} == {"history", "current"}
        assert sum(image["image_role"] == "query_image" for image in records[2]["images"]) == 3


@pytest.mark.parametrize("failure", ["provider", "parse", "budget"])
async def test_failed_or_blocked_calls_never_export_images(environment, monkeypatch, failure):
    root, profile, shots = environment
    client = Client()
    if failure == "provider":
        async def fail(*a, **k):
            raise RuntimeError("provider unavailable")
        client.complete = fail
    elif failure == "parse":
        async def malformed(*a, **k):
            return "invalid"
        client.complete = client.repair_json = malformed
    else:
        profile.long_screenshot.max_input_tokens = 1
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    items, errors = parse_jsonl(json.dumps({"query": "q", "screenshot1": shots[0], "screenshot2": shots[1]}), "compare")
    assert not errors
    task = Task(id="failure", mode="compare", items=items, options={})
    one, _ = runner._make_item_evaluator(task, AppConfig(
        judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile}))
    result = await one(0, items[0])
    assert result.get("error") and "model_input_evidence" not in result
    path = root / "failure.zip"
    with pytest.raises(ValueError, match="没有可导出"):
        write_model_input_zip(history.task_to_snapshot(task), path)
    assert not path.exists()


async def test_export_uses_latest_results_and_never_mutable_item_audit(environment, monkeypatch):
    task, _ = await mixed_task(environment, monkeypatch)
    snapshot = history.task_to_snapshot(task)
    snapshot["items"][0]["screenshot_evidence"] = deepcopy(snapshot["items"][1]["screenshot_evidence"])
    before = deepcopy(snapshot)
    records, skipped = successful_records(snapshot)
    assert records[0]["evidence_mode"] == "video_frames"
    snapshot["results"].append({"index": 0, "error": "later retry failed"})
    records, skipped = successful_records(snapshot)
    assert [record["item_id"] for record in records] == ["shots", "query-video"]
    assert {record["item_id"] for record in skipped} == {"video", "failed"}
    snapshot["results"].pop()
    write_model_input_zip(snapshot, environment[0] / "stable.zip")
    assert snapshot == before


async def test_concurrent_case_capture_does_not_mix_requests(environment, monkeypatch):
    root, profile, shots = environment
    objects = [{"id": f"case-{n}", "query": f"question-{n}",
                "screenshot1": shots[n], "screenshot2": shots[n + 1]} for n in (0, 1)]
    items, errors = parse_jsonl(json.dumps(objects), "compare")
    assert not errors
    client = Client()
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    task = Task(id="concurrent", mode="compare", items=items, options={"concurrency": 2})
    one, _ = runner._make_item_evaluator(task, AppConfig(
        judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile}))
    await asyncio.gather(*(one(index, item) for index, item in enumerate(items)))
    records, skipped = successful_records(history.task_to_snapshot(task))
    assert not skipped and len(records) == 2
    assert records[0]["request_id"] != records[1]["request_id"]
    for record in records:
        call = next(call for call in client.calls if record["query"] in call[1])
        assert [Path(image["evidence_path"]).read_bytes() for image in record["images"]] == sent_images(call)


async def test_rich_content_uses_same_exact_request_export(environment, monkeypatch):
    root, profile, shots = environment
    client = Client()
    async def complete(system, user, **kwargs):
        client.calls.append((system, user, kwargs))
        return json.dumps({"answer_coverage": "complete"})
    client.complete = complete
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    task = Task(id="rich-images", mode="rich_content", items=[
        {"id": "rich", "query": "q", "frames": [shots[0]]}], options={})
    one, _ = runner._make_item_evaluator(task, AppConfig(
        judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile}))
    result = await one(0, task.items[0])
    assert not result.get("error"), result
    with zipfile.ZipFile(write_model_input_zip(history.task_to_snapshot(task), root / "rich.zip")) as archive:
        record = json.loads(archive.read("manifest.json"))["records"][0]
        assert [archive.read(image["archive_path"]) for image in record["images"]] == sent_images(client.calls[0])


@pytest.mark.parametrize("changed", [True, False])
async def test_missing_or_changed_frozen_input_fails_cleanly(environment, monkeypatch, changed):
    root, _, _ = environment
    task, _ = await mixed_task(environment, monkeypatch)
    path = Path(task.results[0]["model_input_evidence"]["images"][0]["evidence_path"])
    if changed:
        path.write_bytes(b"changed frozen evidence")
    else:
        path.unlink()
    destination = root / "incomplete.zip"
    with pytest.raises(EvidenceUnavailable):
        write_model_input_zip(history.task_to_snapshot(task), destination)
    assert not destination.exists()


async def test_task_download_endpoint_streams_zip_and_cleans_file(environment, monkeypatch):
    root, _, _ = environment
    task, client = await mixed_task(environment, monkeypatch)
    monkeypatch.setattr(history, "HISTORY_DIR", root / "history")
    assert history.save_task(task)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as browser:
        response = await browser.get(f"/api/eval/{task.id}/export?format=model_input_evidence")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert "filename*=utf-8" in response.headers["content-disposition"]
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            assert json.loads(archive.read("manifest.json"))["exported_cases"] == len(client.calls)
        assert not list((root / "runs" / "exports").glob("model-input-*.zip"))
        restored = await browser.get(f"/api/history/{task.id}")
        assert restored.status_code == 200
        assert restored.json()["results"][0]["model_input_evidence"]["images"]
        assert (await browser.get("/api/eval/missing/export?format=model_input_evidence")).status_code == 404
        for result in task.results:
            result["error"] = "failed"
        assert history.save_task(task)
        assert (await browser.get(f"/api/eval/{task.id}/export?format=model_input_evidence")).status_code == 404


def test_legacy_video_is_not_reconstructed_and_legacy_successful_screenshot_is_supported(environment):
    root, _, shots = environment
    raw = Path(shots[0]).read_bytes()
    record = evidence_audit.capture_request("system", [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(raw).decode()}}],
        [{"ref_path": shots[0], "image_role": "product_answer"}], [], {})
    record["record_status"] = "model_response_received"
    snapshot = {"items": [{"id": "old-video", "frames1": [shots[0]]},
                           {"id": "old-shot", "screenshot_evidence": record}],
                "results": [{"index": "0"}, {"index": "1"}]}
    records, skipped = successful_records(snapshot)
    assert len(records) == 1 and records[0]["item_id"] == "old-shot"
    assert skipped[0]["reason"] == "request_not_recorded"
    assert write_model_input_zip(snapshot, root / "legacy.zip").exists()
    snapshot["results"] = [{}, {}]
    assert len(successful_records(snapshot)[0]) == 1
