"""Single-turn joint input keeps primary/auxiliary evidence and durable limit failures."""
import copy
import io
import csv
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.query_images import QueryImageError
from auto_eval.web import parse_input, runner, server, video_prepare
from auto_eval.web.tasks import Task


def profile():
    return VisualModeProfile(extraction={"algorithm_version": "test-joint"})


def joint_item(count=2):
    return {"id": "case", "query": "附近有什么餐厅", "product_count": count,
            "evidence_mode": "long_screenshot_with_video",
            **{f"screenshot{n}": f"{n}.png" for n in range(1, count + 1)},
            **{f"video{n}": f"{n}.mp4" for n in range(1, count + 1)}}


@pytest.mark.parametrize("count", [2, 3])
def test_json_and_csv_explicit_joint_mode_preserve_both_layers(count):
    source = joint_item(count)
    source["query_images"] = ["query.png"]
    source["task_start_time"] = 1.0
    items, errors = parse_input.parse_jsonl(json.dumps(source), "compare")
    assert not errors
    normalized = parse_input.normalize_compare_evidence(source)
    assert all(normalized[f"{kind}{n}"] == source[f"{kind}{n}"]
               for kind in ("screenshot", "video") for n in range(1, count + 1))
    assert items[0]["video1"] and items[0]["screenshot1"]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(source))
    writer.writeheader()
    writer.writerow({**source, "query_images": json.dumps(source["query_images"])})
    csv_items, csv_errors = parse_input.parse_csv(stream.getvalue(), "compare")
    assert not csv_errors
    assert csv_items[0]["product_count"] == count
    assert csv_items[0]["source_line"] == 2
    assert csv_items[0]["query_images"] == ["query.png"]
    assert csv_items[0]["evidence_mode"] == "long_screenshot_with_video"


def test_legacy_both_paths_still_choose_screenshots_and_joint_requires_every_product():
    source = joint_item()
    source.pop("evidence_mode")
    normalized = parse_input.normalize_compare_evidence(source)
    assert normalized["evidence_mode"] == "long_screenshot" and "video1" not in normalized
    for field in ("screenshot1", "screenshot2", "video1", "video2"):
        invalid = joint_item()
        invalid[field] = " "
        with pytest.raises(ValueError, match=field):
            parse_input.normalize_compare_evidence(invalid)


def test_multiturn_stays_screenshot_only_and_api_rejects_joint_before_normalizing():
    item = {**joint_item(), "session_id": "session", "turn_index": 1,
            "screenshot_scope": "current_turn"}
    app_cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile()})
    for remove_video in (False, True):
        source = copy.deepcopy(item)
        if remove_video:
            source.pop("video1")
            source.pop("video2")
        imported, errors = parse_input.parse_jsonl(json.dumps(source), "compare")
        assert not imported and any("multiturn_video_unsupported" in error for error in errors)
        with pytest.raises(HTTPException) as raised:
            server._validate_eval_request(server.EvalReq(mode="compare", items=[source]), app_cfg)
        assert raised.value.status_code == 422
        assert "multiturn_video_unsupported" in raised.value.detail
    old = {**item, "evidence_mode": "long_screenshot"}
    old.pop("video1")
    old.pop("video2")
    assert not parse_input.parse_jsonl(json.dumps(old), "compare")[1]


@pytest.mark.parametrize("count", [2, 3])
def test_joint_preparation_keeps_primary_slices_and_reuses_video_cache(tmp_path, count):
    item = joint_item(count)
    calls = []
    for number in range(1, count + 1):
        Image.new("RGB", (80, 140), "white").save(tmp_path / item[f"screenshot{number}"])
        (tmp_path / item[f"video{number}"]).write_bytes(f"video{number}".encode())
    def extract(video, directory, **kwargs):
        calls.append(video.name)
        output = directory / "kf_001.jpg"
        Image.new("RGB", (80, 140), "blue").save(output)
        (directory / "keyframes.json").write_text(json.dumps({"algorithm_version": "test",
            "selected": [{"index": 1, "time": 2.5, "source": "sample", "keep_reason": "stable-state-end"}]}))
        return [output]
    kwargs = dict(profile=profile(), session_name="joint", item_index=0, total_items=1,
        base_dir=tmp_path, runs_dir=tmp_path / "runs", probe_fn=lambda _: 10, extract_fn=extract)
    first = video_prepare.prepare_session_joint_evidence_item(item, **kwargs)
    second = video_prepare.prepare_session_joint_evidence_item(first, **kwargs)
    assert len(calls) == count
    assert first["evidence_mode"] == "long_screenshot_with_video"
    assert first["frame_count"] == count * 2
    assert first["aux_frame_count"] == count
    for n in range(1, count + 1):
        assert first[f"frames{n}"] == [f"{n}.png"]
        assert first[f"frames{n}"] != first[f"aux_frames{n}"]
        assert first[f"aux_frames{n}"] == second[f"aux_frames{n}"]
        meta = first[f"aux_frame_meta{n}"][0]
        assert meta["time_s"] == 2.5
        assert meta["source_video_sha256"] == first[f"video_source{n}"]["sha256"]
        assert meta["sha256"] and meta["keep_reason"] == "stable-state-end"
    (tmp_path / "2.mp4").write_bytes(b"changed")
    updated = video_prepare.prepare_session_joint_evidence_item(second, **kwargs)
    assert len(calls) == count + 1
    assert updated["aux_frames2"] != first["aux_frames2"]
    assert updated["frames2"] == first["frames2"]


def test_joint_auxiliary_frames_stay_inside_declared_task_window(tmp_path):
    item = joint_item()
    item.update(task_start_time=1, task_end_time=5)
    for number in (1, 2):
        Image.new("RGB", (80, 140)).save(tmp_path / f"{number}.png")
        (tmp_path / f"{number}.mp4").write_bytes(b"video")
    def extract(video, directory, **kwargs):
        images = []
        for number in (1, 2, 3):
            output = directory / f"kf_{number:03d}.jpg"
            Image.new("RGB", (80, 140)).save(output)
            images.append(output)
        (directory / "keyframes.json").write_text(json.dumps({
            "selected": [{"index": 1, "time": 0}, {"index": 2, "time": 3}, {"index": 3, "time": 9.7}]}))
        return images
    result = video_prepare.prepare_session_joint_evidence_item(item, profile=profile(), session_name="window",
        item_index=0, total_items=1, base_dir=tmp_path, runs_dir=tmp_path / "runs", probe_fn=lambda _: 10, extract_fn=extract)
    assert result["aux_frame_count"] == 2
    assert all(meta["time_s"] == 3 for number in (1, 2) for meta in result[f"aux_frame_meta{number}"])


def test_joint_preparation_rejects_missing_sample_timestamps(tmp_path):
    source = tmp_path / "kf_001.jpg"
    Image.new("RGB", (80, 140)).save(source)
    with pytest.raises(ValueError, match="元数据"):
        video_prepare._frame_metadata([str(source)], {"sha256": "video-sha"})


@pytest.mark.asyncio
async def test_runner_forwards_auxiliary_groups_with_source_metadata():
    calls = []
    class Judge:
        async def evaluate(self, **kwargs):
            calls.append(kwargs)
            return {"product_count": 3}
    item = joint_item(3)
    for n in (1, 2, 3):
        item.update({f"frames{n}": [f"slice{n}.png"], f"screenshot_meta{n}": {"original_path": f"{n}.png"},
            f"aux_frames{n}": [f"frame{n}.jpg"], f"aux_frame_meta{n}": [{"time_s": n}]})
    await runner._eval_one("compare", 0, item, compare_judges=[Judge()], image_limits={"test": True})
    assert len(calls) == 1
    request = calls[0]
    assert request["evidence_mode"] == "long_screenshot_with_video"
    assert request["frames1"] == ["slice1.png"]
    assert request["aux_frame_groups"] == [["frame1.jpg"], ["frame2.jpg"], ["frame3.jpg"]]
    assert request["aux_frame_metas"][1] == [{"time_s": 2}]
    assert request["image_limits"] == {"test": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [False, True])
async def test_runner_limit_error_is_not_retried_and_is_durable(monkeypatch, provider):
    item = joint_item()
    item.update(frames1=["slice1.png"], frames2=["slice2.png"])
    report = {"status": "blocked", "estimated_input_tokens": 500, "context_window": 300}
    error = RuntimeError("maximum context length exceeded: requested 500 tokens, limit 300") if provider else QueryImageError(
        "context_budget_exceeded", "输入 token 超过上下文限制", request_budget_report=report)
    error.request_budget_report = report
    calls = []
    class Client:
        def __init__(self, *args):
            pass
    async def evaluate(*args, **kwargs):
        calls.append(kwargs)
        raise error
    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "prepare_session_joint_evidence_item", lambda item, **kwargs: item)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *args, **kwargs: None)
    task = Task(id="joint-limit", mode="compare", items=[item], options={})
    app_cfg = AppConfig(judges=[JudgeConfig(name="test", model="fake")], visual_modes={"rich_content": profile()})
    one, _ = runner._make_item_evaluator(task, app_cfg)
    result = await one(0, item)
    assert len(calls) == 1
    assert result["error_type"] == ("context_limit_exceeded" if provider else "context_budget_exceeded")
    assert result["retry_allowed"] is False
    assert result["context_limit_error"]["origin"] == ("provider" if provider else "local_guardrail")
    assert result["request_budget_report"] == report
    assert task.items[0]["request_budget_report"] == report
    assert task.items[0]["context_limit_error"] == result["context_limit_error"]
    assert result["request_id"] and result["warning"]
    assert task.protocol_manifest["joint_evidence_version"] == "single-turn-joint-v1"


@pytest.mark.asyncio
async def test_manual_retry_and_resume_skip_nonretryable_context_errors(monkeypatch):
    task = Task(id="blocked", mode="compare", items=[{"query": "q", "retry_allowed": False}],
        options={}, status="done", results=[{"index": 0, "error": "too long", "retry_allowed": False}])
    async def get(*args):
        return task
    monkeypatch.setattr(server, "get_task_async", get)
    monkeypatch.setattr(server, "_check_judge_options", lambda *args: None)
    with pytest.raises(HTTPException) as raised:
        await server.api_retry_failed(task.id, server.RetryReq())
    assert raised.value.status_code == 409
    assert raised.value.detail["skipped"][0]["reason"] == "request_limit_nonretryable"
    with pytest.raises(HTTPException) as raised:
        await server.api_resume(task.id, server.ResumeReq(concurrency=1, include_failed=True))
    assert raised.value.status_code == 409
    assert raised.value.detail["skipped"][0]["reason"] == "request_limit_nonretryable"


def test_single_item_video_download_does_not_choose_joint_screenshot(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    data = {"items": [{"evidence_mode": "long_screenshot_with_video", "media": ["screenshot.png", str(source)],
        "video1_path": str(source), "video_source1": video_prepare._video_identity(source)}]}
    monkeypatch.setattr(server, "peek_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "load_snapshot", lambda *args: data)
    monkeypatch.setattr(server, "_resolve_operation_video_path", lambda value: Path(value))
    assert server.api_export_item("joint", 0, format="video").path == source


def test_api_submission_discards_untrusted_generated_auxiliary_metadata():
    item = joint_item()
    item.update(aux_frames1=["injected.jpg"], aux_frame_meta1=[{"time_s": 9}],
        context_limit_error={"code": "injected"}, retry_allowed=False)
    app_cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile()})
    request = server.EvalReq(mode="compare", items=[item])
    server._validate_eval_request(request, app_cfg)
    item = request.items[0]
    assert "aux_frames1" not in item and "aux_frame_meta1" not in item
    assert "context_limit_error" not in item and "retry_allowed" not in item
    assert item["video1"] and item["screenshot1"]


def blocked_api_task(monkeypatch, tmp_path):
    cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile()})
    source = joint_item()
    marker = {"code": "context_limit_exceeded", "error_id": "known-error", "retry_allowed": False}
    task = Task(id="blocked-update", mode="compare", items=[{**source, "retry_allowed": False, "context_limit_error": marker}],
        options={}, status="done", results=[{"index": 0, "error": "old context error", "retry_allowed": False,
            "request_id": "previous-call", "context_limit_error": marker}])
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", "")
    monkeypatch.setattr(server, "cfg", lambda: cfg)
    monkeypatch.setattr(server, "get_task", lambda _: task)
    monkeypatch.setattr(server, "_check_judge_options", lambda *args: None)
    monkeypatch.setattr(server, "_freeze_legacy_judge", lambda *args: None)
    monkeypatch.setattr(server, "queue_task_save", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "spawn_background", lambda coro: coro.close())
    return source, task


@pytest.mark.asyncio
async def test_update_endpoint_rejects_unchanged_blocked_input_without_clearing_original(monkeypatch, tmp_path):
    source, task = blocked_api_task(monkeypatch, tmp_path)
    before = copy.deepcopy(task.items)
    # Arbitrary metadata and forged diagnostics cannot turn this into a new evaluation.
    incoming = {**source, "source_data": {"custom": "changed"}, "retry_allowed": True,
                "context_limit_error": {"code": "forged"}}
    with pytest.raises(HTTPException) as raised:
        await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[incoming]))
    assert raised.value.status_code == 409
    assert raised.value.detail["blocked"][0]["context_limit_error"]["error_id"] == "known-error"
    assert task.items == before and task.results[0]["request_id"] == "previous-call"
    assert not task.active_runs and "update_batches" not in task.execution_control


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("task_end_time", 5), ("query", "另一个问题"), ("video2", "changed.mp4")])
async def test_update_endpoint_allows_changed_evaluation_input_and_keeps_limit_audit(monkeypatch, tmp_path, field, value):
    source, task = blocked_api_task(monkeypatch, tmp_path)
    incoming = {**source, field: value}
    response = await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[incoming]))
    assert response["replaced_ids"] == ["case"]
    assert task.items[0][field] == value
    assert "retry_allowed" not in task.items[0]
    audit = task.execution_control["request_limit_reassessments"][0]
    assert audit["previous_request_id"] == "previous-call"
    assert audit["previous_limit_error"]["error_id"] == "known-error"
    assert task.results[0]["error"] == "old context error", "old result remains visible until replacement completes"


@pytest.mark.asyncio
async def test_update_endpoint_detects_changed_source_bytes_at_same_path(monkeypatch, tmp_path):
    source, task = blocked_api_task(monkeypatch, tmp_path)
    image = tmp_path / "1.png"
    Image.new("RGB", (80, 140), "white").save(image)
    task.items[0]["screenshot_meta1"] = {"original_path": "1.png", "original_sha256": server.file_hash(image)}
    Image.new("RGB", (80, 140), "black").save(image)
    response = await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[source]))
    assert response["replaced_ids"] == ["case"]
    assert "retry_allowed" not in task.items[0]


def test_query_snapshot_alias_is_same_evaluation_input(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "BASE_DIR", tmp_path)
    original = tmp_path / "query.png"
    snapshot = tmp_path / "runs" / "original.png"
    snapshot.parent.mkdir()
    Image.new("RGB", (80, 140)).save(original)
    snapshot.write_bytes(original.read_bytes())
    previous = {**joint_item(), "query_images": [str(snapshot)], "query_image_meta": [{
        "original_path": str(snapshot), "source_path": "query.png", "original_sha256": server.file_hash(original)}]}
    incoming = {**joint_item(), "query_images": ["query.png"]}
    assert not server._evaluation_input_changed(previous, incoming, "compare", profile())


@pytest.mark.asyncio
async def test_resume_residual_update_batch_skips_permanent_limit_and_runs_other_case(monkeypatch):
    task = Task(id="residual", mode="compare", items=[
        {"id": "blocked", "query": "q", "retry_allowed": False}, {"id": "other", "query": "new"}],
        options={}, status="paused", active_runs=1,
        results=[{"index": 0, "error": "old context error", "retry_allowed": False, "request_id": "old"}])
    attempt = {"id": "resume-test", "indexes": [0, 1], "concurrency": 1, "completed": 0, "failed": 0}
    task.execution_control.update(state="queued", pending_indexes=[0, 1], resumes=[attempt], update_batches={
        "batch": {"remaining": [0, 1], "options": {}, "prior_summary": ""}})
    calls = []
    def make(parent, cfg, *, options=None, on_result=None):
        async def one(index, item):
            calls.append(index)
            result = {"index": index, "query": item["query"], "item_id": item["id"]}
            await on_result(index, result, 0)
            return result
        return one, []
    async def persist(*args, **kwargs):
        return True
    monkeypatch.setattr(runner, "_make_item_evaluator", make)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "retire_task", lambda *args: None)
    await runner.run_resume(task, object())
    assert calls == [1]
    assert task.results[0]["error"] == "old context error"
    assert task.results[0]["request_id"] == "old"
    assert task.execution_control["update_batch_skips"][0]["reason"] == "request_limit_nonretryable"
    assert attempt["status"] == "completed" and attempt["skipped"] == 1
    assert not task.execution_control["update_batches"]
    assert not task.execution_control["pending_indexes"]
    assert task.status == "done"
