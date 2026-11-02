"""Frozen comprehensive ratings survive platform lifecycle and export routes."""
from copy import deepcopy

import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import history, runner
from auto_eval.web.protocol_identity import recover_protocol_identity, require_snapshot_protocol
from auto_eval.web.tasks import Task, _task_from_snapshot, latest_results_by_index


PROFILE = "dialogue_experience_compare@1.0"
SCHEMA = "dialogue-experience-result-1"


def projected_result(count=2):
    protocol = resolve_compare_protocol(PROFILE)
    result = {
        "product_count": count,
        "evaluation_profile": protocol.id,
        "standard_id": protocol.standard_id,
        "standard_version": protocol.standard_version,
        "bundle_revision": protocol.bundle_revision,
        "result_schema_version": SCHEMA,
        "overall_reason": "；".join(f"产品{n}：本轮主要目标有效满足。" for n in range(1, count + 1)),
    }
    for product in range(1, count + 1):
        result.update({
            f"answer{product}_overall_score": 6 - product,
            f"answer{product}_overall_score_status": "scored",
            f"answer{product}_overall_applicable": True,
            f"answer{product}_overall_reason": f"产品{product}目标满足，评分依据。",
            f"answer{product}_suggestion": f"产品{product}的改进建议",
            f"answer{product}_domain": f"产品{product}的垂域",
        })
    return result


def frozen_snapshot(count=2):
    protocol = resolve_compare_protocol(PROFILE)
    item = {"id": "experience-1", "query": "帮我设置明早7点闹钟", "product_count": count}
    for product in range(1, count + 1):
        item.update({f"answer{product}": f"产品{product}原始回答文本",
                     f"context{product}": f"产品{product}背景"})
    result = dict(projected_result(count), index=0, item_id=item["id"], query=item["query"])
    result.update({f"answer{product}": item[f"answer{product}"] for product in range(1, count + 1)})
    return {
        "task_id": "experience-lifecycle", "mode": "compare", "evaluation_profile": PROFILE,
        "protocol_manifest": protocol.public_metadata(), "options": {}, "items": [item],
        "results": [result], "summary": {"total": 1, "done": 1, "overall_answer1_avg": 5},
    }


def test_frozen_protocol_and_four_output_values_survive_restore():
    source = frozen_snapshot(3)
    before = deepcopy(source)
    recovered = recover_protocol_identity(source)
    assert recovered["is_dialogue_experience"] and recovered["executable"]
    assert require_snapshot_protocol(source).id == PROFILE
    restored = _task_from_snapshot(source, source["task_id"])
    assert restored.protocol_manifest["bundle_revision"] == "1.0.1"
    assert restored.protocol_manifest["result_schema_version"] == SCHEMA
    assert "recovery_source" not in restored.protocol_manifest
    payload = history.snapshot_payload(history.task_to_snapshot(restored))
    assert payload["evaluation_profile"] == PROFILE
    assert payload["protocol_executable"]
    for product in (1, 2, 3):
        for suffix in ("overall_score", "overall_reason", "suggestion", "domain"):
            field = f"answer{product}_{suffix}"
            assert payload["results"][0][field] == source["results"][0][field]
    assert source == before


@pytest.mark.parametrize("change", ["missing_revision", "missing_schema", "unknown_revision", "wrong_schema", "mixed_family"])
def test_incomplete_or_conflicting_identity_keeps_raw_but_blocks_reinterpretation(change):
    source = frozen_snapshot()
    if change.startswith("missing_"):
        key = "bundle_revision" if change == "missing_revision" else "result_schema_version"
        source["protocol_manifest"].pop(key)
        source["results"][0].pop(key)
    elif change == "unknown_revision":
        source["protocol_manifest"]["bundle_revision"] = "99.0.0"
        source["results"][0]["bundle_revision"] = "99.0.0"
    elif change == "wrong_schema":
        source["protocol_manifest"]["result_schema_version"] = "dialogue-experience-result-99"
        source["results"][0]["result_schema_version"] = "dialogue-experience-result-99"
    else:
        source["results"][0]["standard_id"] = "qa_chat_compare"
    before = deepcopy(source)
    recovered = recover_protocol_identity(source)
    assert recovered["is_dialogue_experience"] and not recovered["executable"]
    payload = history.snapshot_payload(source)
    assert not payload["protocol_executable"]
    assert payload["raw_summary"] == source["summary"]
    assert "overall_answer1_avg" not in payload["summary"]
    assert payload["results"] == before["results"] and source == before
    with pytest.raises(ValueError):
        require_snapshot_protocol(source)


@pytest.mark.parametrize("count", [2, 3])
def test_live_summary_history_export_and_single_item_api_use_same_protocol(count):
    source = frozen_snapshot(count)
    task = _task_from_snapshot(source, source["task_id"])
    summary = runner._summarize(task)
    assert summary["overall_answer1_avg"] == 5
    exported = history.export_rows(source)["逐题结果"][0]
    single = history.result_export_row("compare", source["results"][0], 0, source["items"],
                                       PROFILE, source["protocol_manifest"], source["results"])
    assert single == exported
    assert not any("Gate" in key or "内容冲突" in key for key in exported)
    for product in range(1, count + 1):
        assert exported[f"产品{product}综合体验分"] == 6 - product
        assert exported[f"产品{product}综合体验理由"] == source["results"][0][f"answer{product}_overall_reason"]
        assert exported[f"产品{product}提升建议"] == f"产品{product}的改进建议"
        assert exported[f"产品{product}垂域"] == f"产品{product}的垂域"


@pytest.fixture
def experience_runtime(monkeypatch, tmp_path):
    protocol = resolve_compare_protocol(PROFILE)
    cfg = AppConfig(judges=[JudgeConfig(name="judge", model="fake")],
                    visual_modes={"rich_content": VisualModeProfile(name="rich_content", extraction={"algorithm_version": "test"})})
    calls = []

    class Client:
        def __init__(self, _cfg):
            pass

        async def aclose(self):
            pass

    class Judge:
        def __init__(self, _client, _profile, selected_protocol):
            self.protocol = selected_protocol

        async def evaluate(self, **kwargs):
            calls.append({k: v for k, v in kwargs.items() if k != "evidence_callback"})
            callback = kwargs.get("evidence_callback")
            if callback:
                callback({"record_status": "model_response_received", "images": [{"product": 1}]})
            return projected_result(kwargs["product_count"])

    async def persist(_task):
        return True

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "VisualCompareJudge", Judge)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "retire_task", lambda _task: None)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    return cfg, protocol, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("count,reverse", [(2, False), (2, True), (3, False)])
@pytest.mark.parametrize("path", ["run", "resume", "retry", "update"])
async def test_frozen_protocol_survives_all_execution_paths(experience_runtime, count, reverse, path):
    cfg, protocol, calls = experience_runtime
    source = frozen_snapshot(count)
    item = source["items"][0]
    for product in range(1, count + 1):
        item[f"frames{product}"] = [f"already-prepared-product-{product}"]
    task = Task(id="experience-paths", mode="compare", items=[deepcopy(item)],
                options={"reverse_product_order": reverse}, evaluation_profile=PROFILE,
                protocol_manifest=protocol.public_metadata(), active_runs=1)
    await runner.run_eval(task, cfg)
    restored = _task_from_snapshot(deepcopy(history.task_to_snapshot(task)), task.id)
    restored.active_runs = 1
    if path == "run":
        await runner.run_eval(restored, cfg)
    elif path == "resume":
        restored.execution_control = {
            "state": "queued", "pending_indexes": [0],
            "resumes": [{"id": "resume_experience", "status": "queued", "indexes": [0],
                         "concurrency": 1, "completed": 0, "failed": 0}],
        }
        await runner.run_resume(restored, cfg)
    elif path == "retry":
        restored.results = [{"index": 0, "item_id": item["id"], "error": "old failure"}]
        restored.retry_runs["retry_experience"] = {
            "retry_id": "retry_experience", "status": "queued", "indexes": [0],
            "reasons": {"0": "failed"}, "options": {}, "total": 1,
            "completed": 0, "succeeded": 0, "failed": 0, "skipped": 0,
            "items": {"0": {"status": "queued"}},
        }
        await runner.run_retry(restored, cfg, "retry_experience")
        assert restored.retry_runs["retry_experience"]["items"]["0"]["attempt_result"]["result_schema_version"] == SCHEMA
    else:
        await runner.run_update_batch(restored, cfg, [(0, restored.items[0])], options={})
    result = latest_results_by_index(restored)[0]
    assert not result.get("error"), result
    assert len(calls) == 2
    assert all(call.get("reverse_product_order", False) is reverse for call in calls)
    assert result["evaluation_profile"] == PROFILE and result["result_schema_version"] == SCHEMA
    assert result["reverse_product_order"] is reverse
    assert result["evaluation_run_at"]
    assert "evaluation_datetime" not in result
    assert restored.protocol_manifest["result_schema_version"] == SCHEMA
    for product in range(1, count + 1):
        assert result[f"answer{product}"] == item[f"answer{product}"]
        assert result[f"context{product}"] == item[f"context{product}"]


@pytest.mark.asyncio
async def test_runner_forwards_trusted_capture_and_multimodal_target_bundle(experience_runtime):
    _cfg, protocol, _calls = experience_runtime
    source = frozen_snapshot(3)
    item = source["items"][0]
    item.update(query_images=["prepared-query.png"], query_image_meta=[{"path": "prepared-query.png"}],
                evidence_mode="long_screenshot")
    for product in (1, 2, 3):
        item[f"frames{product}"] = [f"prepared-{product}.png"]
        item[f"screenshot_meta{product}"] = {"slices": [{"path": f"prepared-{product}.png"}]}
    captures = []

    class Judge:
        async def evaluate(self, **kwargs):
            captures.append(kwargs)
            return projected_result(3)

    judge = Judge()
    judge.protocol = protocol
    trusted = {"answer2": {"input_status": "failed", "source": "synthetic-collector"}}
    bundle = {"target_turn": 2, "turns": [{"turn_index": 1}, {"turn_index": 2}]}
    result = await runner._eval_one("compare", 0, item, compare_judges=[judge],
                                    conversation_input=bundle, trusted_collection_metadata=trusted)
    assert not result.get("error")
    sent = captures[0]
    assert sent["trusted_collection_metadata"] == trusted
    assert sent["conversation_input"] is bundle
    assert sent["product_count"] == 3 and sent["query_image_meta"] == item["query_image_meta"]
    assert sent["screenshot_metas"] == [item[f"screenshot_meta{p}"] for p in (1, 2, 3)]
    assert sent["evidence_mode"] == "long_screenshot"
    for product in (1, 2, 3):
        assert sent[f"answer{product}"] == item[f"answer{product}"]
        assert sent[f"context{product}"] == item[f"context{product}"]
