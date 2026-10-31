"""Recover formatting in the real judge without another semantic model call."""
from copy import deepcopy
import json

import pytest

from auto_eval.config import VisualModeProfile
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_COMPARE_PROTOCOL_ID as PROFILE,
    CHITCHAT_FLAT_DIMENSIONS as DIMS,
)
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.web.history import snapshot_payload, task_to_snapshot
from auto_eval.web.tasks import Task, _task_from_snapshot
from test_chitchat_flat_runtime import payload
from test_knowledge_qa_platform import Client, FIXED_TIME, payload as knowledge_payload


def source_payload(count=2):
    data = payload(count)
    data["understanding_reason"] = '产品1：原文称"先聊聊"；产品2：原文称"慢慢来"'
    data["answer1_safety_stability_score"] = 1
    data["answer1_style_score"] = 2
    data["answer2_style_score"] = 4
    data["answer2_understanding_score"] = None
    return data


def render(source, kind):
    if kind in {"alias", "both"}:
        aliases = {f"answer{n}_{dimension}_score": f"{dimension}_answer{n}_score"
                   for dimension in DIMS for n in range(1, 4)}
        source = {aliases.get(key, key): value for key, value in source.items()}
    raw = json.dumps(source, ensure_ascii=False)
    return raw.replace('\\"', '"') if kind in {"quotes", "both"} else raw


def judge(client, revision=None):
    profile = VisualModeProfile(extraction={"algorithm_version": "test"})
    return VisualCompareJudge(client, profile, resolve_compare_protocol(PROFILE, revision))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("kind", ["quotes", "alias", "both"])
@pytest.mark.parametrize("revision", ["1.1.0", "1.1.1"])
async def test_recovery_keeps_all_scores_reasons_and_audit_without_rejudging(count, kind, revision):
    source = source_payload(count)
    raw = render(source, kind)
    client = Client(raw)
    result = await judge(client, revision).evaluate(question="陪聊", product_count=count, evaluation_time=FIXED_TIME)
    assert len(client.calls) == 1 and client.repair_calls == 0
    for key, value in source.items():
        assert result[key] == value
    assert result["raw_model_output"] == raw
    assert result["repaired_model_output"] == ""
    assert result["locally_repaired_json"] is True
    assert result["local_format_recovery_version"] == "chitchat-flat-format-recovery-1"
    assert result["local_format_recovery_steps"]
    assert dict(result["locally_repaired_key_pairs"]) == source
    assert result["structural_diagnostics"] == []
    assert result["duplicate_keys"] == []
    if kind == "alias":
        assert dict(result["raw_key_pairs"]) == json.loads(raw)
    else:
        assert result["raw_key_pairs"] == []
    assert result["bundle_revision"] == revision
    assert result["input_manifest_sha256"]


@pytest.mark.asyncio
async def test_clean_output_keeps_request_fingerprint_and_existing_results():
    source = source_payload()
    client = Client(source)
    result = await judge(client).evaluate(question="陪聊", product_count=2, evaluation_time=FIXED_TIME)
    broken_client = Client(render(source, "both"))
    recovered = await judge(broken_client).evaluate(question="陪聊", product_count=2, evaluation_time=FIXED_TIME)
    assert client.calls == broken_client.calls
    assert recovered["prompt_sha256"] == result["prompt_sha256"]
    assert recovered["input_manifest_sha256"] == result["input_manifest_sha256"]
    assert result["locally_repaired_json"] is False
    assert "local_format_recovery_version" not in result
    for key in source:
        assert result[key] == recovered[key] == source[key]


@pytest.mark.asyncio
async def test_recovered_audit_survives_task_history_restore():
    source = source_payload()
    raw = render(source, "both")
    protocol = resolve_compare_protocol(PROFILE)
    result = await judge(Client(raw)).evaluate(question="陪聊", product_count=2, evaluation_time=FIXED_TIME)
    result.update(index=0, item_id="synthetic")
    task = Task(id="format-recovery-history", mode="compare", items=[{"id": "synthetic", "query": "陪聊"}],
                options={}, evaluation_profile=PROFILE, protocol_manifest=protocol.public_metadata(), results=[result])
    snapshot = task_to_snapshot(task)
    before = deepcopy(snapshot)
    restored = _task_from_snapshot(json.loads(json.dumps(snapshot)), task.id)
    saved = snapshot_payload(task_to_snapshot(restored))["results"][0]
    for key in ("raw_model_output", "local_format_recovery_version", "local_format_recovery_steps",
                "locally_repaired_key_pairs", "raw_key_pairs", "locally_repaired_json"):
        assert saved[key] == result[key]
    assert snapshot == before


@pytest.mark.asyncio
async def test_recovered_output_respects_existing_product_order_mapping():
    source = source_payload()
    raw = render(source, "alias")
    result = await judge(Client(raw)).evaluate(
        question="陪聊", product_count=2, evaluation_time=FIXED_TIME, reverse_product_order=True)
    assert result["answer1_style_score"] == source["answer2_style_score"]
    assert result["answer2_style_score"] == source["answer1_style_score"]
    assert result["answer2_safety_stability_score"] == 1
    assert result["answer1_understanding_score"] is None
    assert result["judge_product_order"] == [2, 1]
    assert result["raw_model_output"] == raw
    assert dict(result["locally_repaired_key_pairs"]) == source


@pytest.mark.asyncio
async def test_recovery_never_fills_missing_scores_or_calls_model_repair():
    source = source_payload()
    source.pop("answer1_style_score")
    raw = render(source, "both")
    client = Client(raw)
    with pytest.raises(JudgeOutputParseError) as caught:
        await judge(client).evaluate(question="陪聊", product_count=2)
    assert caught.value.raw_output == raw
    assert caught.value.structural_diagnostics
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
async def test_knowledge_qa_keeps_its_independent_strict_parser():
    source = knowledge_payload()
    source["understanding_reason"] = '产品1：原文"完整"；产品2：原文"完整"'
    raw = json.dumps(source, ensure_ascii=False).replace('\\"', '"')
    client = Client(raw)
    profile = VisualModeProfile(extraction={"algorithm_version": "test"})
    with pytest.raises(JudgeOutputParseError):
        await VisualCompareJudge(client, profile, "knowledge_qa_compare@1.0").evaluate(question="问题", product_count=2)
    assert len(client.calls) == 1 and client.repair_calls == 0
