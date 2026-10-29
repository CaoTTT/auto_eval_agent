"""End-to-end knowledge QA protocol checks with synthetic model responses."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from auto_eval import query_images
from auto_eval.config import JudgeConfig
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID, list_compare_protocols, resolve_compare_protocol,
)
from auto_eval.judges.knowledge_qa_schema import KNOWLEDGE_QA_DIMENSIONS as DIMS
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.long_screenshot import prepare_long_screenshot
from auto_eval.web import server
from auto_eval.web.protocol_identity import recover_protocol_identity
from auto_eval.web.tasks import _task_from_snapshot
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_vqa_input import setup_images as image_setup


PROFILE = "knowledge_qa_compare@1.0"
FIXED_TIME = datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)


def payload(count=2):
    result = {}
    for dimension in DIMS:
        result[dimension + "_reason"] = "；".join(
            f"产品{n}：当前轮截图顶部可见答案，核心信息完整且没有该维实质缺陷，符合2分要求。"
            for n in range(1, count + 1)
        )
        for n in range(1, count + 1):
            result[f"answer{n}_{dimension}_status"] = "scored"
            result[f"answer{n}_{dimension}_score"] = 2
    return result


class Client:
    persona = "测试裁判"
    model = "fake-knowledge-judge"
    cfg = JudgeConfig(name="test", model="fake-knowledge-judge")

    def __init__(self, data):
        self.raw = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self.calls = []
        self.repair_calls = 0

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.raw

    async def repair_json(self, *args, **kwargs):
        self.repair_calls += 1
        raise AssertionError("知识协议不能二次调用模型修补评分")


def args_for_images(root, profile, paths, count=2, mode="long_screenshot"):
    args = dict(question="QUERY_SENTINEL", evidence_mode=mode, product_count=count,
                evaluation_time=FIXED_TIME)
    metas = []
    for n in range(1, count + 1):
        args[f"context{n}"] = f"[产品已知信息]P{n}_BACKGROUND\n[仅供裁判核对]P{n}_REFERENCE"
        args[f"answer{n}"] = f"P{n}_COPIED_ANSWER"
        if mode == "long_screenshot":
            meta = prepare_long_screenshot(Path(paths[n]), root / f"slice-{n}", profile.long_screenshot)
            metas.append(meta)
            args[f"frames{n}"] = [part["path"] for part in meta["slices"]]
        else:
            args[f"frames{n}"] = [paths[n]]
    if metas:
        args["screenshot_metas"] = metas
    return args


def test_protocol_is_selectable_and_preserves_default_identity():
    protocol = server._compare_protocol_or_422(PROFILE, require_selectable=True)
    assert protocol.id == PROFILE and protocol.standard_id == "knowledge_qa_compare"
    assert protocol.bundle_revision == "1.0.0"
    assert protocol.result_schema_version == "knowledge-qa-result-2"
    assert protocol.dimensions == protocol.score_dimensions == DIMS
    assert not protocol.require_response_pass
    metadata = protocol.public_metadata()
    assert metadata["allowed_scores_by_dimension"]["accuracy"] == [0, 1, 1.5, 2]
    assert metadata["score_range"] == [0, 2]
    assert len([p for p in list_compare_protocols() if p.id == PROFILE]) == 1
    assert resolve_compare_protocol(None).id == DEFAULT_COMPARE_PROTOCOL_ID
    with pytest.raises(ValueError):
        resolve_compare_protocol(PROFILE, "99.0.0")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
@pytest.mark.parametrize("with_query_image", [False, True])
async def test_real_judge_path_preserves_four_scores_and_original_media(
    image_setup, count, mode, with_query_image,
):
    root, profile, paths = image_setup
    prepared = query_images.prepare_query_images(
        {"query": "QUERY_SENTINEL", "query_images": [paths[0]] if with_query_image else []},
        session_name="knowledge-platform", cfg=profile.query_images,
    )
    data = payload(count)
    data.update(answer1_accuracy_score=0, answer2_accuracy_score=1.5)
    client = Client(data)
    args = args_for_images(root, profile, paths, count, mode)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **args, query_image_meta=prepared["query_image_meta"],
    )
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert result["product_count"] == count
    assert result["evaluation_profile"] == PROFILE
    assert result["bundle_revision"] == "1.0.0"
    assert result["result_schema_version"] == "knowledge-qa-result-2"
    assert result["answer1_accuracy_score"] == 0
    assert result["answer2_accuracy_score"] == 1.5
    assert type(result["answer2_accuracy_score"]) is float
    assert result["answer1_clarity_and_structure_score"] == 2
    assert json.loads(result["raw_model_output"]) == data
    assert result["input_manifest_sha256"]
    assert not any(key.endswith(("_gate", "_winner", "_rank_groups", "_evidence")) for key in result)
    assert "has_conflict" not in result and "total_score" not in result
    assert any(key.startswith("answer3_") for key in result) is (count == 3)
    system, user, sent = client.calls[0]
    assert sent.get("require_complete") is True
    assert "QUERY_SENTINEL" in user and "证据位置" in system
    for n in range(1, count + 1):
        assert f"P{n}_BACKGROUND" in user and f"P{n}_COPIED_ANSWER" in user
    parts = sent["content_parts"]
    assert len([p for p in parts if p["type"] == "image_url"]) == count + int(with_query_image)
    assert len(sent["image_metadata"]) == count + int(with_query_image)
    labels = "\n".join(p.get("text", "") for p in parts)
    for n in range(1, count + 1):
        assert f"产品{n}" in labels
    if with_query_image:
        assert "共享用户题目，不是产品回答" in labels


@pytest.mark.asyncio
async def test_status_and_missing_evidence_are_local_to_product_and_dimension(image_setup):
    root, profile, paths = image_setup
    data = payload(3)
    data.update(answer1_accuracy_status="unverifiable", answer1_accuracy_score=None,
                answer2_multimedia_value_status="not_applicable", answer2_multimedia_value_score=None,
                answer3_content_quality_status="insufficient_evidence", answer3_content_quality_score=None)
    result = await VisualCompareJudge(Client(data), profile, PROFILE).evaluate(
        **args_for_images(root, profile, paths, 3),
    )
    for product, dimension, status in ((1, "accuracy", "unverifiable"),
                                      (2, "multimedia_value", "not_applicable"),
                                      (3, "content_quality", "insufficient_evidence")):
        prefix = f"answer{product}_{dimension}"
        assert result[prefix + "_status"] == result[prefix + "_score_status"] == status
        assert result[prefix + "_score"] is None
    assert result["answer2_accuracy_score"] == 2
    assert result["answer1_multimedia_value_score"] == 2
    assert result["answer3_clarity_and_structure_score"] == 2


@pytest.mark.asyncio
async def test_trusted_capture_failure_does_not_create_a_product_gate(image_setup):
    root, profile, paths = image_setup
    data = payload(3)
    trusted = {"answer2": {"input_status": "failed", "source": "test collector", "capture_id": "capture-2"}}
    client = Client(data)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **args_for_images(root, profile, paths, 3), trusted_collection_metadata=trusted,
    )
    assert result["answer2_execution_status"] == "input_failed"
    assert result["answer2_input_status"] == "failed"
    for dimension in DIMS:
        assert result[f"answer2_{dimension}_score"] is None
        assert result[f"answer2_{dimension}_status"] == "insufficient_evidence"
        assert result[f"answer1_{dimension}_score"] == result[f"answer3_{dimension}_score"] == 2
    assert json.loads(result["raw_model_output"]) == data
    assert not any(key.endswith("_gate") for key in result)


@pytest.mark.asyncio
async def test_trusted_collection_state_changes_request_fingerprint(image_setup):
    root, profile, paths = image_setup
    args = args_for_images(root, profile, paths)
    client = Client(payload())
    judge = VisualCompareJudge(client, profile, PROFILE)
    first = await judge.evaluate(**args)
    second = await judge.evaluate(**args, trusted_collection_metadata={
        "answer1": {"input_status": "partial", "source": "test collector"},
    })
    assert first["prompt_sha256"] == second["prompt_sha256"]
    assert first["input_manifest_sha256"] != second["input_manifest_sha256"]
    assert first["answer1_accuracy_score"] == second["answer1_accuracy_score"] == 2


def malformed_outputs():
    valid = payload()
    missing = deepcopy(valid)
    missing.pop("accuracy_reason")
    invalid_status = deepcopy(valid)
    invalid_status["answer1_accuracy_status"] = "scored"
    invalid_status["answer1_accuracy_score"] = None
    invalid_half = deepcopy(valid)
    invalid_half["answer1_content_quality_score"] = 1.5
    invalid_bool = deepcopy(valid)
    invalid_bool["answer1_accuracy_score"] = True
    extra = {**valid, "accuracy_evidence": []}
    duplicate = json.dumps(valid)[:-1] + ', "answer1_accuracy_score": 0}'
    return [json.dumps(x) for x in (missing, invalid_status, invalid_half, invalid_bool, extra)] + [
        duplicate, json.dumps(valid)[:-1], "[]", "", '```json\n' + json.dumps(valid) + '\n```',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", malformed_outputs())
async def test_illegal_model_output_is_a_technical_failure_without_second_judgment(image_setup, raw):
    root, profile, paths = image_setup
    client = Client(raw)
    with pytest.raises(JudgeOutputParseError):
        await VisualCompareJudge(client, profile, PROFILE).evaluate(**args_for_images(root, profile, paths))
    assert len(client.calls) == 1
    assert client.repair_calls == 0


@pytest.mark.asyncio
async def test_multiturn_judge_keeps_target_and_own_history_without_gate_instructions(conversation_data):
    items, profile, _ = conversation_data
    parsed, preparation = prepare_conversation(conversation_data)
    target = parsed[1]
    bundle = preparation.prepare(target)
    client = Client(payload())
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        question=target["query"], product_count=2, evidence_mode="long_screenshot",
        frames1=[part["path"] for part in target["screenshot_meta1"]["slices"]],
        frames2=[part["path"] for part in target["screenshot_meta2"]["slices"]],
        screenshot_metas=[target["screenshot_meta1"], target["screenshot_meta2"]],
        conversation_input=bundle, evaluation_time=FIXED_TIME,
    )
    system, _, sent = client.calls[0]
    assert "原有 Schema、适用性、Gate" not in "\n".join(p.get("text", "") for p in sent["content_parts"])
    text = "\n".join(p.get("text", "") for p in sent["content_parts"])
    assert "问题1" in text and "问题2" in text and "问题3" not in text
    assert "产品1自己的回答1" in text and "产品2自己的回答2" in text
    assert "证据位置" in system
    assert result["input_manifest_sha256"]
    assert result["answer1_accuracy_score"] == 2


def frozen_snapshot():
    protocol = resolve_compare_protocol(PROFILE)
    return {"mode": "compare", "evaluation_profile": PROFILE, "protocol_manifest": protocol.public_metadata(),
            "items": [], "results": []}


def test_frozen_knowledge_identity_can_restore_task_without_legacy_inference():
    snapshot = frozen_snapshot()
    recovered = recover_protocol_identity(snapshot)
    assert recovered["executable"] and recovered["is_knowledge_qa"]
    restored = _task_from_snapshot(snapshot, "knowledge-restored")
    assert restored.evaluation_profile == PROFILE
    assert restored.protocol_manifest["result_schema_version"] == "knowledge-qa-result-2"
    assert "recovery_source" not in restored.protocol_manifest


@pytest.mark.parametrize("missing", ["bundle_revision", "result_schema_version"])
def test_incomplete_knowledge_identity_never_infers_a_current_runtime(missing):
    snapshot = frozen_snapshot()
    snapshot["protocol_manifest"].pop(missing)
    recovered = recover_protocol_identity(snapshot)
    assert not recovered["executable"] and recovered["diagnostics"]


def test_wrong_knowledge_schema_cannot_resume_or_enter_formal_statistics():
    snapshot = frozen_snapshot()
    snapshot["protocol_manifest"]["result_schema_version"] = "knowledge-qa-result-1"
    recovered = recover_protocol_identity(snapshot)
    assert not recovered["executable"] and recovered["diagnostics"]
