"""Exercise the new flat chat protocol through the real judge with fake responses."""
from copy import deepcopy
import json

import pytest

from auto_eval import query_images
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_COMPARE_PROTOCOL_ID as PROFILE,
    CHITCHAT_FLAT_DIMENSIONS as DIMS,
    expected_output_keys,
)
from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID, QA_CHAT_V2_CALIBRATED_COMPARE_PROTOCOL_ID,
    list_compare_protocols, resolve_compare_protocol,
)
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.web import server
from test_knowledge_qa_platform import Client, FIXED_TIME, args_for_images
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation
from test_qa_chat_v2_protocol import payload as previous_payload
from test_vqa_input import setup_images as image_setup


def payload(count=2):
    data = {}
    for dimension in DIMS:
        data[f"{dimension}_reason"] = "；".join(
            f"产品{n}：本轮复制文本自然承接用户陪聊请求，内容完整且符合该维要求"
            for n in range(1, count + 1)
        )
        for n in range(1, count + 1):
            data[f"answer{n}_{dimension}_score"] = 5
    return data


def test_selectable_label_has_seven_dimensions_and_preserves_old_protocols():
    protocol = server._compare_protocol_or_422(PROFILE, require_selectable=True)
    assert protocol.display == "闲聊七维·证据先行·1-5分（候选）"
    assert protocol.bundle_revision == "1.1.1"
    assert protocol.result_schema_version == "chitchat-flat-result-1"
    assert protocol.dimensions == protocol.score_dimensions == DIMS
    assert len([p for p in list_compare_protocols() if p.id == PROFILE]) == 1
    assert resolve_compare_protocol(None).id == DEFAULT_COMPARE_PROTOCOL_ID
    assert resolve_compare_protocol(QA_CHAT_V2_CALIBRATED_COMPARE_PROTOCOL_ID).bundle_revision == "1.1.3"
    metadata = protocol.public_metadata()
    assert metadata["has_gates"] is False
    assert metadata["allowed_scores_by_dimension"]["response_experience"] == [1, 5]
    assert metadata["allowed_scores_by_dimension"]["style"] == [1, 2, 3, 4, 5]
    with pytest.raises(ValueError):
        resolve_compare_protocol(PROFILE, "99.0.0")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
@pytest.mark.parametrize("with_query_image", [False, True])
async def test_existing_inputs_and_media_are_unchanged(image_setup, count, mode, with_query_image):
    root, profile, paths = image_setup
    prepared = query_images.prepare_query_images(
        {"query": "QUERY_SENTINEL", "query_images": [paths[0]] if with_query_image else []},
        session_name="flat-chat-platform", cfg=profile.query_images,
    )
    args = args_for_images(root, profile, paths, count, mode)
    args["query_image_meta"] = prepared["query_image_meta"]
    data = payload(count)
    data.update(answer1_safety_stability_score=1, answer1_understanding_score=1,
                answer2_style_score=None)
    client = Client(data)
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(**args)
    old_client = Client(previous_payload(count))
    await VisualCompareJudge(old_client, profile, QA_CHAT_V2_CALIBRATED_COMPARE_PROTOCOL_ID).evaluate(**args)
    assert len(client.calls) == 1 and client.repair_calls == 0
    system, user, sent = client.calls[0]
    _, old_user, old_sent = old_client.calls[0]
    assert user[:user.rfind("按各产品自身证据")] == old_user[:old_user.rfind("按各产品自身证据")]
    for field in ("user_images", "user_image_refs", "image_metadata"):
        assert sent.get(field) == old_sent.get(field)
    if "content_parts" in sent:
        assert sent["content_parts"][1:] == old_sent["content_parts"][1:]
    assert sent["require_complete"] is True
    assert "单层扁平JSON" in system
    assert result["answer1_safety_stability_score"] == 1
    assert result["answer1_understanding_score"] == 1
    assert result["answer1_style_score"] == 5
    assert result["answer2_style_score"] is None
    assert result["answer2_style_score_status"] == "unverifiable"
    assert result["evaluation_profile"] == PROFILE
    assert result["input_manifest_sha256"]
    assert json.loads(result["raw_model_output"]) == data
    assert list(result)[:len(data)] == list(expected_output_keys(count))
    assert not any(key.endswith(("_gate", "_winner", "_rank_groups", "_evidence", "_total_score")) for key in result)
    assert "has_conflict" not in result
    assert not any("personalization" in key for key in result)
    assert any(key.startswith("answer3_") for key in result) is (count == 3)


@pytest.mark.asyncio
async def test_capture_failure_remains_separate_from_product_safety(image_setup):
    root, profile, paths = image_setup
    data = payload(3)
    data["answer1_safety_stability_score"] = 1
    client = Client(data)
    trusted = {"answer2": {"input_status": "failed", "source": "test collector"}}
    result = await VisualCompareJudge(client, profile, PROFILE).evaluate(
        **args_for_images(root, profile, paths, 3), trusted_collection_metadata=trusted,
    )
    for dim in DIMS:
        assert result[f"answer2_{dim}_score"] is None
        assert result[f"answer3_{dim}_score"] == 5
    assert result["answer1_safety_stability_score"] == 1
    assert result["answer1_style_score"] == 5
    assert json.loads(result["raw_model_output"]) == data
    assert result["normalization_diagnostics"]


def malformed_outputs():
    valid = payload()
    missing = deepcopy(valid)
    missing.pop("understanding_reason")
    bad_gate = {**valid, "answer1_safety_stability_score": 3}
    bad_bool = {**valid, "answer1_style_score": True}
    bad_string = {**valid, "answer1_style_score": "4"}
    extra = {**valid, "answer1_response_gate": "pass"}
    duplicate = json.dumps(valid)[:-1] + ', "answer1_style_score": 1}'
    return [json.dumps(x) for x in (missing, bad_gate, bad_bool, bad_string, extra)] + [
        duplicate, json.dumps(valid)[:-1], "[]", "", '```json\n' + json.dumps(valid) + '\n```',
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", malformed_outputs())
async def test_invalid_output_preserves_raw_and_never_rejudges(image_setup, raw):
    root, profile, paths = image_setup
    client = Client(raw)
    with pytest.raises(JudgeOutputParseError) as caught:
        await VisualCompareJudge(client, profile, PROFILE).evaluate(**args_for_images(root, profile, paths))
    assert len(client.calls) == 1 and client.repair_calls == 0
    assert caught.value.raw_output == raw
    assert caught.value.structural_diagnostics


@pytest.mark.asyncio
async def test_diagnostic_parser_failure_still_preserves_raw_and_request_identity(image_setup):
    root, profile, paths = image_setup
    raw = json.dumps(payload()).replace('"answer1_style_score": 5', '"answer1_style_score": ' + '9' * 5000)
    client = Client(raw)
    with pytest.raises(JudgeOutputParseError) as caught:
        await VisualCompareJudge(client, profile, PROFILE).evaluate(**args_for_images(root, profile, paths))
    assert caught.value.raw_output == raw
    assert caught.value.raw_model_output == raw
    assert caught.value.input_manifest_sha256
    assert caught.value.structural_diagnostics
    assert len(client.calls) == 1 and client.repair_calls == 0


@pytest.mark.asyncio
async def test_multiturn_keeps_history_and_scores_only_target_without_old_gate_instruction(conversation_data):
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
    _, _, sent = client.calls[0]
    text = "\n".join(p.get("text", "") for p in sent["content_parts"])
    assert "问题1" in text and "问题2" in text and "问题3" not in text
    assert "产品1自己的回答1" in text and "产品2自己的回答2" in text
    assert "target_turn=2 的七维扁平JSON" in text
    assert "原有 Schema、适用性、Gate" not in text
    assert result["answer1_response_experience_score"] == 5
