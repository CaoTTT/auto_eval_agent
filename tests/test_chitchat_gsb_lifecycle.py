"""Frozen categorical GSB survives task restore and all execution entry points."""
from copy import deepcopy
from itertools import combinations

import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.judges.chitchat_gsb_schema import (
    finalize_chitchat_gsb_observation, validate_output,
)
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import history, runner
from auto_eval.web.protocol_identity import recover_protocol_identity, require_snapshot_protocol
from auto_eval.web.tasks import Task, _task_from_snapshot, latest_results_by_index


PROFILE = "chitchat_gsb_compare@1.0"
SCHEMA = "chitchat-gsb-result-1"


def projected_result(count=2):
    protocol = resolve_compare_protocol(PROFILE)
    decisions = {"1_2": "G", "1_3": "B", "2_3": "S"}
    wire = {
        f"pair_{a}_{b}": {
            "status": "evaluated", "gsb": decisions[f"{a}_{b}"],
            "reason": f"产品{a}与产品{b}的可见回复存在上述体验差异，按本轮主要诉求形成整体结论。",
            "serious_issue": "none",
        }
        for a, b in combinations(range(1, count + 1), 2)
    }
    return finalize_chitchat_gsb_observation(validate_output(wire, count), protocol)


def frozen_snapshot(count=2):
    protocol = resolve_compare_protocol(PROFILE)
    item = {"id": "gsb-lifecycle-1", "query": "我只想吐槽，别给建议。", "product_count": count}
    for product in range(1, count + 1):
        item.update({f"answer{product}": f"产品{product}原始回答正文",
                     f"context{product}": f"产品{product}明确背景",
                     f"frames{product}": [f"already-prepared-product-{product}"]})
    result = dict(projected_result(count), index=0, item_id=item["id"], query=item["query"])
    return {
        "task_id": "gsb-lifecycle", "mode": "compare", "evaluation_profile": PROFILE,
        "protocol_manifest": protocol.public_metadata(), "options": {}, "items": [item],
        "results": [result], "summary": {"total": 1, "done": 1, "gsb_pairs": {
            "1_2": {"valid_count": 1, "G": 1, "S": 0, "B": 0, "net_win_rate": 1}}},
    }


@pytest.fixture
def gsb_runtime(monkeypatch, tmp_path):
    protocol = resolve_compare_protocol(PROFILE)
    cfg = AppConfig(judges=[JudgeConfig(name="judge", model="fake-gsb")],
                    visual_modes={"rich_content": VisualModeProfile(
                        name="rich_content", extraction={"algorithm_version": "test"})})
    calls, clients_created = [], []

    class Client:
        def __init__(self, _cfg):
            clients_created.append(_cfg.model)

        async def aclose(self):
            pass

    class Judge:
        def __init__(self, _client, _profile, selected_protocol):
            self.protocol = selected_protocol
            assert (selected_protocol.id, selected_protocol.bundle_revision,
                    selected_protocol.result_schema_version) == (PROFILE, "1.0.0", SCHEMA)

        async def evaluate(self, **kwargs):
            calls.append({key: value for key, value in kwargs.items() if key != "evidence_callback"})
            callback = kwargs.get("evidence_callback")
            if callback:
                callback({"record_status": "model_response_received", "images": [{"product": 1}]})
            # The judge's API returns final original-product identities, including
            # when its internal presentation order was reversed.
            return projected_result(kwargs["product_count"])

    async def persist(_task):
        return True

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "VisualCompareJudge", Judge)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_persist_task_and_wait", persist)
    monkeypatch.setattr(runner, "retire_task", lambda _task: None)
    monkeypatch.setattr(runner, "RUNS_DIR", tmp_path)
    return cfg, protocol, calls, clients_created


@pytest.mark.asyncio
@pytest.mark.parametrize("count,reverse", [(2, False), (2, True), (3, False)])
@pytest.mark.parametrize("path", ["run", "resume", "retry", "update"])
async def test_frozen_gsb_protocol_and_categorical_results_survive_execution_paths(gsb_runtime, count, reverse, path):
    cfg, protocol, calls, _clients = gsb_runtime
    item = frozen_snapshot(count)["items"][0]
    task = Task(id="gsb-paths", mode="compare", items=[deepcopy(item)],
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
            "resumes": [{"id": "resume_gsb", "status": "queued", "indexes": [0],
                         "concurrency": 1, "completed": 0, "failed": 0}],
        }
        await runner.run_resume(restored, cfg)
    elif path == "retry":
        restored.results = [{"index": 0, "item_id": item["id"], "error": "old failure"}]
        restored.retry_runs["retry_gsb"] = {
            "retry_id": "retry_gsb", "status": "queued", "indexes": [0],
            "reasons": {"0": "failed"}, "options": {}, "total": 1,
            "completed": 0, "succeeded": 0, "failed": 0, "skipped": 0,
            "items": {"0": {"status": "queued"}},
        }
        await runner.run_retry(restored, cfg, "retry_gsb")
        attempt = restored.retry_runs["retry_gsb"]["items"]["0"]["attempt_result"]
        assert attempt["result_schema_version"] == SCHEMA
        assert attempt["gsb_pairs"]["1_2"]["gsb"] == "G"
    else:
        await runner.run_update_batch(restored, cfg, [(0, restored.items[0])], options={})
    result = latest_results_by_index(restored)[0]
    assert not result.get("error"), result
    assert len(calls) == 2
    assert all(call.get("reverse_product_order", False) is reverse for call in calls)
    assert all(call["product_count"] == count for call in calls)
    assert result["evaluation_profile"] == PROFILE
    assert result["bundle_revision"] == "1.0.0"
    assert result["result_schema_version"] == SCHEMA
    assert result["reverse_product_order"] is reverse
    assert result["judge_product_order"] == ([2, 1] if reverse else list(range(1, count + 1)))
    assert result["evaluation_run_at"]
    assert "evaluation_datetime" not in result
    assert restored.protocol_manifest["result_schema_version"] == SCHEMA
    assert result["gsb_pairs"] == projected_result(count)["gsb_pairs"]
    assert not any(key.endswith(("_score", "_rank_groups", "_response_gate", "_safety_gate")) for key in result)
    for product in range(1, count + 1):
        assert result[f"answer{product}"] == item[f"answer{product}"]
        assert result[f"context{product}"] == item[f"context{product}"]
    summary = runner._summarize(restored)
    assert summary["total"] == summary["done"] == summary["comparable"] == 1
    for key, value in projected_result(count)["gsb_pairs"].items():
        metrics = summary["gsb_pairs"][key]
        assert metrics["valid_count"] == 1
        assert sum(metrics[label] for label in ("G", "S", "B")) == 1
        assert metrics[value["gsb"]] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_complete_result_metadata_recovers_missing_manifest_without_upgrading(gsb_runtime, count):
    cfg, _protocol, calls, _clients = gsb_runtime
    source = frozen_snapshot(count)
    source.pop("protocol_manifest")
    source.pop("evaluation_profile")
    before = deepcopy(source)
    recovered = recover_protocol_identity(source)
    assert recovered["is_chitchat_gsb"] and recovered["executable"]
    assert recovered["evaluation_profile"] == PROFILE
    restored = _task_from_snapshot(source, source["task_id"])
    assert restored.evaluation_profile == PROFILE
    assert restored.protocol_manifest["id"] == PROFILE
    assert restored.protocol_manifest["bundle_revision"] == "1.0.0"
    assert restored.protocol_manifest["result_schema_version"] == SCHEMA
    assert "recovery_source" not in restored.protocol_manifest
    assert require_snapshot_protocol(history.task_to_snapshot(restored)).id == PROFILE
    assert source == before
    restored.active_runs = 1
    await runner.run_eval(restored, cfg)
    assert len(calls) == 1
    assert runner._summarize(restored)["gsb_pairs"]["1_2"]["valid_count"] == 1


@pytest.mark.parametrize("change", [
    "missing_version", "unknown_version", "missing_revision", "unknown_revision", "missing_schema", "unknown_schema",
])
def test_unrecoverable_gsb_identity_blocks_execution_but_preserves_viewable_results(gsb_runtime, change):
    cfg, _protocol, calls, clients = gsb_runtime
    source = frozen_snapshot()
    if change == "missing_version":
        source.pop("evaluation_profile")
        source["protocol_manifest"].pop("id")
        source["protocol_manifest"].pop("standard_version")
        source["results"][0].pop("evaluation_profile")
        source["results"][0].pop("standard_version")
    elif change == "unknown_version":
        source["evaluation_profile"] = source["protocol_manifest"]["id"] = "chitchat_gsb_compare@9.0"
        source["protocol_manifest"]["standard_version"] = "9.0"
        source["results"][0].update(evaluation_profile="chitchat_gsb_compare@9.0", standard_version="9.0")
    elif change.startswith("missing_"):
        key = "bundle_revision" if change == "missing_revision" else "result_schema_version"
        source["protocol_manifest"].pop(key)
        source["results"][0].pop(key)
    else:
        key, value = ("bundle_revision", "9.0.0") if change == "unknown_revision" else (
            "result_schema_version", "chitchat-gsb-result-99")
        source["protocol_manifest"][key] = source["results"][0][key] = value
    before = deepcopy(source)
    recovered = recover_protocol_identity(source)
    assert recovered["is_chitchat_gsb"] and not recovered["executable"]
    payload = history.snapshot_payload(source)
    assert not payload["protocol_executable"]
    assert payload["results"] == before["results"]
    assert payload["raw_summary"] == before["summary"]
    assert "gsb_pairs" not in payload["summary"]
    restored = _task_from_snapshot(deepcopy(source), source["task_id"])
    with pytest.raises(ValueError):
        runner._make_item_evaluator(restored, cfg)
    assert not calls and not clients
    assert history.snapshot_payload(history.task_to_snapshot(restored))["results"] == before["results"]
    assert source == before


@pytest.mark.asyncio
async def test_gsb_runner_forwards_trusted_collection_multimodal_and_target_turn_bundle(gsb_runtime):
    _cfg, protocol, _calls, _clients = gsb_runtime
    item = frozen_snapshot(3)["items"][0]
    item.update(query_images=["prepared-query.png"], query_image_meta=[{"path": "prepared-query.png"}],
                evidence_mode="long_screenshot", include_prior_knowledge=True)
    for product in (1, 2, 3):
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
    assert sent["product_count"] == 3
    assert sent["query_image_meta"] == item["query_image_meta"]
    assert sent["include_prior_knowledge"] is True
    assert sent["screenshot_metas"] == [item[f"screenshot_meta{p}"] for p in (1, 2, 3)]
    assert sent["evidence_mode"] == "long_screenshot"
