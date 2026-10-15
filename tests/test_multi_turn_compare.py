import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image

from auto_eval import query_images
from auto_eval.config import AppConfig, JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.conversation import ConversationIndex, is_conversation, validate_conversations
from auto_eval.image_limits import resolve_image_limits, inspect_asset
from auto_eval.judges.conversation_prompt import assemble_conversation
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import conversation_prepare, runner, server, history
from auto_eval.web.conversation_prepare import ConversationPreparation
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.tasks import Task, _task_from_snapshot
from test_vqa_input import Client


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    monkeypatch.setattr(query_images, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(conversation_prepare, "RUNS_DIR", tmp_path / "runs")
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    profile.query_images.allowed_roots = [str(tmp_path)]
    judge = JudgeConfig(name="test", model="qwen3.5-plus", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    items = []
    for t in range(1, 4):
        item = dict(id=f"s-t{t}", session_id="s", turn_index=t, query=f"问题{t}",
                    product_count=2, screenshot_scope="current_turn", query_images=[])
        for n in (1, 2, 3):
            path = tmp_path / f"p{n}-t{t}.png"
            Image.new("RGB", (64, 96), (n * 50, t * 50, 0)).save(path)
            if n < 3:
                item[f"screenshot{n}"] = str(path)
                item[f"answer{n}"] = f"产品{n}自己的回答{t}"
        items.append(item)
    items[0]["query_images"] = [items[0]["screenshot1"]]
    items[2]["query_images"] = [items[2]["screenshot1"], items[2]["screenshot2"]]
    return items, profile, judge


@pytest.mark.parametrize("change", [
    lambda x: x[1].update(turn_index=True), lambda x: x[1].update(turn_index=0),
    lambda x: x[1].update(turn_index=1), lambda x: x.pop(0), lambda x: x.pop(1),
    lambda x: x[1].pop("session_id"), lambda x: x[1].pop("turn_index"),
    lambda x: x[1].pop("screenshot_scope"), lambda x: x[1].update(screenshot_scope="cumulative"),
    lambda x: x[1].update(video1="movie.mp4"), lambda x: x[1].update(evidence_mode="video_frames"),
    lambda x: x[1].update(media=[{"type":"video", "path":"movie"}]),
    lambda x: x[1].update(frames1=["forged"]), lambda x: x[1].update(id=x[0]["id"]),
    lambda x: x[1].update(product_count=3), lambda x: x[1].update(query1="different"),
    lambda x: x[1].update(query=""),
])
def test_invalid_session_never_silently_drops_a_turn(data, change):
    items, _, _ = data
    change(items)
    parsed, errors = parse_jsonl(json.dumps(items), "compare")
    assert not parsed and errors


def test_unparseable_line_blocks_partial_multiturn_import(data):
    items, _, _ = data
    parsed, errors = parse_jsonl("\n".join(map(json.dumps, items)) + "\n{invalid", "compare")
    assert not parsed and errors


def prepare(data):
    items, profile, judge = data
    parsed, errors = parse_jsonl(json.dumps(items), "compare")
    assert not errors
    limits = resolve_image_limits(judge, profile)
    service = ConversationPreparation(parsed, "test", profile, limits)
    return parsed, service


def request(bundle):
    protocol = resolve_compare_protocol(None)
    system = protocol.system_template.render(persona="test", product_count=bundle["turns"][-1]["product_count"], evidence_mode="long_screenshot")
    return assemble_conversation(system, bundle, protocol)


def test_prefix_order_roles_hashes_and_no_future(data):
    original = deepcopy(data[0])
    data[0].reverse()
    items, service = prepare(data)
    target = next(it for it in items if it["turn_index"] == 2)
    bundle = service.prepare(target)
    system, parts, metadata, refs, fingerprint = request(bundle)
    assert [t["turn_index"] for t in bundle["turns"]] == [1, 2]
    assert [m["asset_id"] for m in metadata] == ["T01-QI01", "P1-T01-A01-S01", "P1-T02-A01-S01", "P2-T01-A01-S01", "P2-T02-A01-S01"]
    assert target["effective_input_modality"] == "text_image"
    assert target["request_budget_report"]["actual_image_count"] == 5
    text = "\n".join(p.get("text", "") for p in parts)
    assert "问题3" not in text and "回答3" not in text and "target_turn=2" in text
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert base64.b64decode(urls[0].split(",")[1]) == Path(original[0]["query_images"][0]).read_bytes()
    assert [m["request_role"] for m in metadata] == ["history", "history", "current", "history", "current"]
    for it in items:
        it.update(turn_summary="DO NOT LEAK", rationale="DO NOT LEAK", answer1_understanding_score=1)
    restored = ConversationPreparation(items, "retry", data[1], service.limits)
    assert request(restored.prepare(target))[-1] == fingerprint
    assert "DO NOT LEAK" not in str(request(restored.prepare(target)))


def test_three_products_three_turns_and_shared_images_once(data):
    for item in data[0]:
        item.update(product_count=3, screenshot3=item["screenshot1"].replace("p1-", "p3-"), answer3="第三产品")
    items, service = prepare(data)
    _, parts, metadata, _, _ = request(service.prepare(items[2]))
    assert len(metadata) == 12  # 3 shared question images + 9 answers
    assert sum(m["image_role"] == "query_image" for m in metadata) == 3
    assert {m["product_no"] for m in metadata if m["image_role"] == "product_answer"} == {1, 2, 3}


def test_append_keeps_prior_fingerprint_and_changed_evidence_is_blocked(data):
    items, service = prepare(data)
    fingerprint = request(service.prepare(items[1]))[-1]
    future = {**items[2], "id": "s-t4", "turn_index": 4, "query": "future"}
    extended = ConversationPreparation([*items, future], "same", data[1], service.limits)
    assert request(extended.prepare(items[1]))[-1] == fingerprint
    Path(service.cache["s-t1"][0]["screenshot1"]).write_bytes(b"changed")
    with pytest.raises(Exception):
        service.prepare(items[1])
    assert items[1]["input_diagnostic_status"] == "blocked"


def test_query_limit_is_durable_and_blocks_dependent_turns(data):
    data[1].query_images.max_pixels = 100
    items, service = prepare(data)
    for item in items[:2]:
        with pytest.raises(Exception):
            service.prepare(item)
        assert item["image_blocking_count"]
        assert item["image_findings"][0]["limit_kind"] == "local_guardrail"
    assert items[0]["image_warning_refs"] == items[1]["image_warning_refs"]


def test_lossless_split_findings_keep_identity_and_pixels(data):
    data[0][0]["query_images"] = []
    data[1].long_screenshot.max_pixels = 64 * 48
    items, service = prepare(data)
    bundle = service.prepare(items[1])
    request(bundle)
    assert items[1]["image_warning_count"] == 4
    assert all(f["status"] == "resolved_by_lossless_split" for f in items[1]["image_findings"])
    meta = bundle["turns"][0]["screenshot_meta1"]
    joined = Image.new("RGB", (64, 96))
    for part in meta["slices"]:
        with Image.open(part["path"]) as im:
            joined.paste(im, (0, part["start_y"]))
    with Image.open(meta["original_path"]) as original:
        assert joined.tobytes() == original.tobytes()


def test_unknown_provider_not_assigned_bailian_limits(data):
    data[2].base_url = "https://api.siliconflow.cn/v1"
    items, service = prepare(data)
    with pytest.raises(Exception, match="未核验"):
        service.prepare(items[0])
    assert service.limits["effective_pixel_cap"] is None
    assert items[0]["image_findings"][0]["code"] == "image_limits_unverified"


def test_total_request_budget_checked_without_new_query_images(data):
    data[1].query_images.max_request_images = 4
    items, service = prepare(data)
    bundle = service.prepare(items[1])
    with pytest.raises(Exception, match="完整历史请求"):
        request(bundle)
    assert items[1]["request_budget_report"]["actual_image_count"] == 5
    assert items[1]["input_diagnostic_status"] == "blocked"


async def test_runner_failure_does_not_block_later_turn_and_restore_third_only(data, monkeypatch):
    items, profile, judge = data
    parsed, errors = parse_jsonl(json.dumps(items), "compare")
    client = Client()
    complete = client.complete
    calls = 0
    async def fail_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("test judge failure")
        return await complete(*args, **kwargs)
    client.complete = fail_first
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    monkeypatch.setattr(runner, "_persist_task", lambda *a: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **k: None)
    cfg = AppConfig(judges=[judge], visual_modes={"rich_content": profile})
    task = Task(id="multi", mode="compare", items=parsed, options={}, session_name="multi")
    await runner._run(task, cfg)
    assert len(task.results) == 3
    assert task.results[0].get("error")
    assert not task.results[1].get("error"), task.results[1]
    assert not task.results[2].get("error"), task.results[2]
    snapshot = history.task_to_snapshot(task)
    from auto_eval.web.screenshot_evidence import evidence_records
    records = evidence_records(snapshot)
    assert len(records) == 3
    assert len(records[2]["images"]) == 9
    assert {im["source_turn"] for im in records[2]["images"]} == {1, 2, 3}
    assert records[2]["images"][0]["request_role"] == "history"
    assert records[2]["record_status"] == "model_response_received"
    restored = _task_from_snapshot(snapshot, task.id)
    one, _ = runner._make_item_evaluator(restored, cfg)
    result = await one(2, deepcopy(restored.items[2]))
    assert not result.get("error"), result
    assert result["input_manifest_sha256"] == task.results[2]["input_manifest_sha256"]
    assert result["request_budget_report"]["actual_image_count"] == 9
    sheets = history.export_rows(snapshot)
    assert sheets["逐题结果"][2]["历史轮数"] == 2
    assert "多轮会话统计" in sheets and "图片检查明细" in sheets


async def test_preflight_checks_full_prefix_without_model_calls(data, monkeypatch):
    items, profile, judge = data
    cfg = AppConfig(judges=[judge], visual_modes={"rich_content": profile})
    monkeypatch.setattr(server, "cfg", lambda: cfg)
    report = await server.api_compare_preflight(server.EvalReq(mode="compare", items=items))
    assert report["accepted_session_count"] == 1
    assert report["blocked_turn_count"] == 0, report
    assert [r["request_budget_report"]["actual_image_count"] for r in report["turns"]] == [3, 5, 9]


def test_direct_api_rejects_video_before_field_cleanup(data):
    items, profile, judge = data
    items[1]["media"] = [{"type": "video", "path": "a"}]
    with pytest.raises(HTTPException, match="multiturn_video_unsupported"):
        server._validate_eval_request(server.EvalReq(mode="compare", items=items), AppConfig(judges=[judge]))


@pytest.mark.parametrize("metadata", [
    {}, {"session_id": "legacy-business-id"}, {"session_id": None},
    {"session_id": "", "turn_index": ""},
    {"session_id": "legacy-business-id", "turn_index": None},
    {"session_id": "legacy-business-id", "turn_index": "  "},
    {"turn_index": None},
])
@pytest.mark.parametrize("evidence", ["video", "screenshot"])
def test_legacy_metadata_import_and_direct_api_remain_single_turn(metadata, evidence):
    original = {"id": "old", "query": "旧评测问题", "product_count": 2,
                f"{evidence}1": "a.mp4" if evidence == "video" else "a.png",
                f"{evidence}2": "b.mp4" if evidence == "video" else "b.png", **metadata}
    parsed, errors = parse_jsonl(json.dumps([original]), "compare")
    assert not errors and len(parsed) == 1
    assert not is_conversation(parsed[0])
    assert "session_id" not in parsed[0] and "turn_index" not in parsed[0]
    assert parsed[0]["source_data"] == original
    for inputs in ([deepcopy(original)], parsed):
        req = server.EvalReq(mode="compare", items=inputs)
        server._validate_eval_request(req, AppConfig(judges=[JudgeConfig(name="test", model="test")]))
        assert not is_conversation(req.items[0])
        assert "session_id" not in req.items[0] and "turn_index" not in req.items[0]
        assert all(req.items[0]["source_data"][key] == value for key, value in original.items())


def test_legacy_session_metadata_cannot_bypass_single_turn_image_limit():
    item = {"query": "q", "session_id": "business", "query_images": ["a.png", "b.png"]}
    with pytest.raises(ValueError, match="0 或 1"):
        query_images.normalize_query_input(item)


def test_legacy_row_with_same_business_id_survives_invalid_multiturn_group(data):
    items, _, _ = data
    items[1]["turn_index"] = 0
    legacy = {"id": "legacy", "query": "single", "session_id": "s", "video1": "a.mp4", "video2": "b.mp4"}
    parsed, errors = parse_jsonl(json.dumps([*items, legacy]), "compare")
    assert errors and len(parsed) == 1
    assert parsed[0]["id"] == "legacy" and not is_conversation(parsed[0])


def test_invalid_session_does_not_erase_other_complete_sessions(data):
    items, _, _ = data
    good = [{**it, "session_id": "good", "id": "good-" + it["id"]} for it in items]
    items[1]["query"] = ""
    parsed, errors = parse_jsonl(json.dumps([*items, *good]), "compare")
    assert errors and len(parsed) == 3
    assert {it["session_id"] for it in parsed} == {"good"}


def test_multiturn_source_data_remains_original(data):
    raw = deepcopy(data[0])
    parsed, errors = parse_jsonl(json.dumps(raw), "compare")
    assert not errors
    assert [it["source_data"] for it in parsed] == raw
    assert all(it["session_group"] == "compare:s" for it in parsed)


def test_session_equal_weight_uses_same_paired_turns_and_ratio_of_means():
    from test_compare_statistics import make_snapshot
    from auto_eval.web.compare_statistics import conversation_statistics
    snapshot = make_snapshot([[5, 1], [5, 1], [1, 5], [4, None]])
    for i, item in enumerate(snapshot["items"]):
        item.update(session_id="long" if i in (0, 1, 3) else "short", turn_index={0:1,1:2,2:1,3:3}[i])
    aligned = history._aligned_results(snapshot, history._results_with_identity(snapshot))
    tables = conversation_statistics(snapshot, aligned)
    record = next(dict(zip(tables[1].headers, row)) for row in tables[1].rows
                  if row[2] == "理解需求" and row[3] == "1/2")
    assert record["有效配对轮数"] == 3
    assert record["有效会话数"] == 2
    assert record["A会话均值"] == record["B会话均值"] == 3
    assert record["会话等权分位值"] == 1


@pytest.mark.parametrize("offset,blocked", [(-1, True), (0, False), (1, False)])
def test_request_image_count_boundary(data, offset, blocked):
    data[1].query_images.max_request_images = 5 + offset
    items, service = prepare(data)
    bundle = service.prepare(items[1])
    if blocked:
        with pytest.raises(Exception):
            request(bundle)
    else:
        request(bundle)
    assert items[1]["request_budget_report"]["blocked"] is blocked


def test_data_uri_bytes_inspected_separately_from_original_file(data):
    items, profile, judge = data
    path = Path(items[0]["screenshot1"])
    raw_length = path.stat().st_size
    profile.long_screenshot.max_data_url_bytes = raw_length + 1
    asset, findings = inspect_asset(path, {"session_id":"s", "source_turn":1, "asset_id":"p1", "product_no":1,
        "image_role":"product_answer"}, resolve_image_limits(judge, profile))
    assert asset["file_bytes"] < profile.long_screenshot.max_data_url_bytes
    assert any(f["metric"] == "data_uri_bytes" and f["limit_kind"] == "local_guardrail" for f in findings)


async def test_retry_endpoint_does_not_expand_multiturn_dependencies(data, monkeypatch):
    items, profile, judge = data
    validate_conversations(items)
    task = Task(id="retry", mode="compare", items=items, options={}, status="done",
        protocol_manifest=server._protocol_manifest(resolve_compare_protocol(None), AppConfig(judges=[judge]), {}),
        results=[{"index":0, "error":"failed"}, {"index":1}, {"index":2, "error":"failed"}])
    async def get_task(_): return task
    class Scheduler:
        def enqueue_retry(self, *args, **kwargs): return 1
    monkeypatch.setattr(server, "get_task_async", get_task)
    monkeypatch.setattr(server, "EVAL_SCHEDULER", Scheduler())
    monkeypatch.setattr(server, "cfg", lambda: AppConfig(judges=[judge], visual_modes={"rich_content":profile}))
    monkeypatch.setattr(server, "_freeze_legacy_judge", lambda *a: None)
    report = await server.api_retry_failed(task.id, server.RetryReq(indexes=[2]))
    assert report["accepted_indexes"] == [2]


async def test_append_api_validates_full_session_and_rejects_replacement(data, monkeypatch):
    items, profile, judge = data
    cfg = AppConfig(judges=[judge], visual_modes={"rich_content": profile})
    task = Task(id="append", mode="compare", items=deepcopy(items[:2]), options={}, status="done",
                evaluation_profile=resolve_compare_protocol(None).id,
                protocol_manifest=server._protocol_manifest(resolve_compare_protocol(None), cfg, {}))
    monkeypatch.setattr(server, "get_task", lambda _: task)
    monkeypatch.setattr(server, "cfg", lambda: cfg)
    monkeypatch.setattr(server, "_freeze_legacy_judge", lambda *a: None)
    monkeypatch.setattr(server, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(server, "spawn_background", lambda coro: coro.close())
    with pytest.raises(HTTPException, match="不可原地覆盖"):
        await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[deepcopy(items[0])]))
    report = await server.api_eval_items(server.EvalItemsReq(task_id=task.id, items=[deepcopy(items[2])]))
    assert report["added_ids"] == ["s-t3"] and report["total_items"] == 3


def test_human_baseline_requires_matching_history(data):
    from auto_eval.web.human_compare import _identity
    item = {**data[0][0], "history_prefix_sha256":"abc"}
    case = {"query":item["query"]}
    assert _identity(case, item, "p1", 1)[0] == "history_unverified"
    case["history_prefix_sha256"] = "different"
    assert _identity(case, item, "p1", 1)[0] == "content_mismatch"


def test_missing_historical_asset_reports_actual_source_turn(data):
    data[0][0]["query_images"] = []
    Path(data[0][0]["screenshot1"]).unlink()
    items, service = prepare(data)
    with pytest.raises(Exception):
        service.prepare(items[2])
    assert items[2]["image_findings"][0]["source_turn"] == 1
    assert items[2]["image_findings"][0]["asset_id"] == "P1-T01-A01"


def test_xlsx_keeps_all_current_query_originals(data):
    import io
    import zipfile
    items, service = prepare(data)
    for item in items:
        service.prepare(item)
    snapshot = {"mode":"compare", "items":items, "results":[], "protocol_manifest":resolve_compare_protocol(None).public_metadata()}
    with zipfile.ZipFile(io.BytesIO(history.build_xlsx(snapshot))) as archive:
        worksheets = "\n".join(archive.read(name).decode() for name in archive.namelist() if name.startswith("xl/worksheets/"))
        assert "输入图片原图2" in worksheets
        assert "多轮会话统计" in archive.read("xl/workbook.xml").decode()
