"""Synthetic contract checks: no network or real judge model is used."""
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from auto_eval.config import JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID, DIMENSIONS, QA_CHAT_COMPARE_PROTOCOL_ID,
    QA_CHAT_DIMENSIONS, resolve_compare_protocol, resolve_compare_standard,
)
from auto_eval.judges.qa_chat_schema import QAChatObservation
from auto_eval.judges.visual_compare_judge import VisualCompareJudge, visual_compare_result_fields
from auto_eval.judges.visual_compare_prompt_qa_chat import QA_CHAT_SOURCE_ANCHORS
from auto_eval.long_screenshot import prepare_long_screenshot


def observation_data(count=2):
    data = {"product_count": count, "rationale": "当前回答完成交流需要", "has_conflict": "no"}
    for product in range(1, count + 1):
        prefix = f"answer{product}"
        data[f"{prefix}_input_status"] = "complete"
        for gate in ("response", "safety"):
            data[f"{prefix}_{gate}_gate"] = "pass"
            data[f"{prefix}_{gate}_gate_reason"] = "产品当前截图展示完整，内容无异常"
        for dimension in QA_CHAT_DIMENSIONS:
            field = f"{prefix}_{dimension}"
            data.update({
                f"{field}_applicable": True, f"{field}_verification_status": "verified",
                f"{field}_score_status": "scored", f"{field}_score": 5 if product == 1 else 3,
                f"{field}_primary_issue": "", f"{field}_reason": f"产品{product} {dimension}当前截图上半部可见依据",
                f"{field}_evidence": [f"产品{product} T01 当前截图上半部原话"],
            })
    return data


def result(data):
    return visual_compare_result_fields(QAChatObservation.model_validate(data), QA_CHAT_COMPARE_PROTOCOL_ID)


def test_new_protocol_is_independent_and_does_not_change_default():
    protocol = resolve_compare_protocol(QA_CHAT_COMPARE_PROTOCOL_ID)
    assert resolve_compare_protocol(None).id == DEFAULT_COMPARE_PROTOCOL_ID
    assert protocol.observation_model is QAChatObservation
    assert protocol.dimensions == protocol.score_dimensions == QA_CHAT_DIMENSIONS
    assert DIMENSIONS == ("understanding", "accuracy", "service_closure", "scenario_fulfillment", "intuitive_efficiency", "evidence_quality", "guided_recommendation")
    metadata = protocol.public_metadata()
    assert metadata["result_schema_version"] == "qa-chat-result-1"
    assert metadata["score_range"] == [1, 5]
    assert metadata["conversation_adapter_version"] == "compare-conversation-1"
    assert metadata["dimensions"][1] == {"key": "personalization", "label": "个性化适配"}
    assert resolve_compare_standard("qa_chat_compare", "1.0", "1.0.0") is protocol
    assert resolve_compare_standard(None, QA_CHAT_COMPARE_PROTOCOL_ID) is protocol
    assert protocol.request_metadata() == metadata
    legacy = resolve_compare_protocol(None)
    assert set(legacy.request_metadata()) == {
        "id", "standard_id", "standard_version", "bundle_revision", "display", "status",
        "score_range", "modes", "input_modalities", "conversation_adapter_version",
    }
    assert "dimensions" in legacy.public_metadata()
    assert "dimensions" not in legacy.request_metadata()
    with pytest.raises(ValueError, match="冲突"):
        resolve_compare_standard("qa_competitor_compare", QA_CHAT_COMPARE_PROTOCOL_ID)
    with pytest.raises(ValueError, match="无法恢复"):
        resolve_compare_protocol(QA_CHAT_COMPARE_PROTOCOL_ID, "1.0.1")


@pytest.mark.parametrize("score", [0, 6, True, False, "4", 4.0, 3.5])
def test_scores_are_strict_integers(score):
    data = observation_data()
    data["answer1_understanding_score"] = score
    with pytest.raises(ValidationError):
        QAChatObservation.model_validate(data)


@pytest.mark.parametrize("missing", [
    "answer1_response_gate", "answer1_response_gate_reason", "answer1_input_status",
    "answer1_understanding_applicable", "answer1_understanding_verification_status",
    "answer1_understanding_score_status", "answer1_understanding_score",
    "answer1_understanding_reason", "answer1_understanding_evidence", "answer1_understanding_primary_issue",
])
def test_active_product_fields_must_be_explicit(missing):
    data = observation_data()
    data.pop(missing)
    with pytest.raises(ValidationError, match="必填"):
        QAChatObservation.model_validate(data)


@pytest.mark.parametrize("changes", [
    {"answer1_understanding_score": None},
    {"answer1_understanding_applicable": None},
    {"answer1_understanding_verification_status": None},
    {"answer1_understanding_score_status": "unknown"},
    {"answer1_understanding_reason": ""},
    {"answer1_understanding_primary_issue": None},
    {"unknown_field": True},
])
def test_invalid_contract_is_a_validation_error(changes):
    data = observation_data()
    data.update(changes)
    with pytest.raises(ValidationError):
        QAChatObservation.model_validate(data)


@pytest.mark.parametrize("gate", ["response", "safety"])
def test_gate_failure_blocks_only_corresponding_product_and_preserves_applicability(gate):
    data = observation_data(3)
    data[f"answer3_{gate}_gate"] = "fail"
    data["answer3_personalization_applicable"] = False
    data["answer3_style_applicable"] = None
    output = result(data)
    for dimension in QA_CHAT_DIMENSIONS:
        assert output[f"answer3_{dimension}_score_status"] == "gate_blocked"
        assert output[f"answer3_{dimension}_score"] is None
        assert output[f"answer1_{dimension}_score"] == 5
        assert output[f"answer2_{dimension}_score"] == 3
        assert output[f"{dimension}_winner"] == "answer1"
        assert output[f"{dimension}_rank_groups"] == []
    assert output["answer3_understanding_applicable"] is True
    assert output["answer3_personalization_applicable"] is False
    assert output["answer3_style_applicable"] is None


def test_input_failure_has_priority_over_gate_failure():
    data = observation_data(3)
    data["answer3_input_status"] = "failed"
    data["answer3_response_gate"] = "fail"
    output = result(data)
    assert output["answer3_response_gate"] == output["answer3_safety_gate"] == "unclear"
    assert output["answer3_understanding_score_status"] == "input_failed"
    assert output["answer3_understanding_verification_status"] is None
    assert output["answer1_understanding_score"] == 5
    assert output["needs_human_review"]
    assert QAChatObservation.model_validate(output).model_dump() == output


def test_unclear_gates_partial_input_and_partial_verification_keep_legal_scores():
    data = observation_data()
    data["answer1_response_gate"] = "unclear"
    data["answer1_safety_gate"] = "unclear"
    data["answer1_input_status"] = "partial"
    data["answer1_understanding_verification_status"] = "partial"
    output = result(data)
    assert output["answer1_understanding_score"] == 5
    assert output["understanding_winner"] == "answer1"
    assert output["needs_human_review"]


@pytest.mark.parametrize("state", ["unverifiable", "not_applicable"])
def test_product_local_empty_score_does_not_clear_another_product(state):
    data = observation_data()
    field = "answer2_personalization"
    data[field + "_score_status"] = state
    data[field + "_applicable"] = False if state == "not_applicable" else True
    data[field + "_verification_status"] = "not_required" if state == "not_applicable" else "unverifiable"
    output = result(data)
    assert output[field + "_score"] is None
    assert output["answer1_personalization_score"] == 5
    assert output["personalization_applicable"] is True
    assert output["personalization_winner"] is None
    assert output["personalization_rank_groups"] == []


def test_derived_fields_do_not_apply_legacy_personalization_projection():
    data = observation_data()
    data["personalization_verification_status"] = "unverifiable"
    data["answer1_personalization_reason"] = "已知不喝咖啡，所以提供无咖啡因饮料"
    data["answer1_scenario_fulfillment_reason"] = "提供了可以直接执行的步骤"
    output = result(data)
    assert "不喝咖啡" in output["personalization_reason"]
    assert "直接执行" not in output["personalization_reason"]
    assert output["answer1_personalization_score"] == 5
    assert any("共享核验摘要" in message for message in output["review_reasons"])
    assert "personalization" not in output
    assert "accuracy_reason" not in output
    assert output["answer3_personalization_score"] is None
    assert output["answer3_personalization_evidence"] == []


def test_frozen_identity_comes_from_protocol_and_json_roundtrip_preserves_fields():
    data = observation_data(3)
    data.update(standard_id="spoof", standard_version="wrong", bundle_revision="wrong", evaluation_profile="wrong")
    output = result(data)
    assert output["standard_id"] == "qa_chat_compare"
    assert output["bundle_revision"] == "1.0.0"
    assert output["evaluation_profile"] == QA_CHAT_COMPARE_PROTOCOL_ID
    assert output["understanding_rank_groups"] == [["product1"], ["product2", "product3"]]
    recovered = QAChatObservation.model_validate(json.loads(json.dumps(output)))
    assert recovered.answer1_personalization_score == 5
    assert recovered.answer3_style_score_status == "scored"


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
def test_complete_anchors_and_output_example_render_for_all_request_modes(count, mode):
    protocol = resolve_compare_protocol(QA_CHAT_COMPARE_PROTOCOL_ID)
    system = protocol.system_template.render(product_count=count, evidence_mode=mode, persona="test")
    source = Path("docs/qa-chat-compare-v1-design.md").read_text(encoding="utf-8")
    anchors = "### A. 响应体验" + source.split("### A. 响应体验", 1)[1].split("## 附录B.", 1)[0]
    assert QA_CHAT_SOURCE_ANCHORS == anchors
    assert anchors in system
    assert "未满足用户的实际需求" in system
    assert "只有重大现实伤害才fail" in system
    assert "[仅供裁判核对]" in system
    assert "不重复处罚已消除的问题" in system
    for dimension in QA_CHAT_DIMENSIONS:
        assert f'"answer{count}_{dimension}_score_status"' in system
    example = json.loads(system[system.index('{\n  "product_count"'):])
    QAChatObservation.model_validate(example)


class FakeClient:
    persona = "test"
    model = "fake-model"
    cfg = JudgeConfig(name="fake")

    def __init__(self, data):
        self.data = data
        self.calls = []
        self.repairs = []

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return json.dumps(self.data, ensure_ascii=False)

    async def repair_json(self, *args, **kwargs):
        self.repairs.append(args)
        pytest.fail("Well-formed JSON must not trigger additional model calls")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["long_screenshot", "video_frames"])
async def test_one_fake_call_uses_existing_media_and_canonical_output(tmp_path, count, mode):
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    client = FakeClient(observation_data(count))
    kwargs = {"question": "按我不喝咖啡的偏好推荐饮料", "product_count": count, "evidence_mode": mode}
    if mode == "long_screenshot":
        metas = []
        for product in range(1, count + 1):
            path = tmp_path / f"p{product}.png"
            Image.new("RGB", (64, 96), "white").save(path)
            meta = prepare_long_screenshot(path, tmp_path / str(product), profile.long_screenshot)
            metas.append(meta)
            kwargs[f"frames{product}"] = [part["path"] for part in meta["slices"]]
        kwargs["screenshot_metas"] = metas
    output = await VisualCompareJudge(client, profile, QA_CHAT_COMPARE_PROTOCOL_ID).evaluate(**kwargs)
    assert len(client.calls) == 1 and not client.repairs
    assert output["answer1_personalization_score"] == 5
    assert output["result_schema_version"] == "qa-chat-result-1"
    assert output["judge_model"] == "fake-model"
    assert output["prompt_sha256"]
    if mode == "long_screenshot":
        assert len(client.calls[0][2]["image_metadata"]) == count
        assert "user_images" not in client.calls[0][2]


@pytest.mark.asyncio
async def test_schema_failure_remains_technical_output_error():
    data = observation_data()
    data.pop("answer2_style_score_status")
    client = FakeClient(data)
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    with pytest.raises(JudgeOutputParseError, match="字段不合法"):
        await VisualCompareJudge(client, profile, QA_CHAT_COMPARE_PROTOCOL_ID).evaluate(question="q")
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("target_turn", [2, 3])
async def test_real_conversation_pipeline_preserves_chat_schema_and_raw_prefix(tmp_path, monkeypatch, count, target_turn):
    from auto_eval import query_images
    from auto_eval.image_limits import resolve_image_limits
    from auto_eval.web import conversation_prepare
    from auto_eval.web.conversation_prepare import ConversationPreparation
    from auto_eval.web.parse_input import parse_jsonl

    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    monkeypatch.setattr(query_images, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(conversation_prepare, "RUNS_DIR", tmp_path / "runs")
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    profile.query_images.allowed_roots = [str(tmp_path)]
    model = JudgeConfig(name="limits-only", model="qwen3.5-plus", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    rows = []
    for turn in range(1, 5):
        row = dict(id=f"chat-t{turn}", session_id="chat-session", turn_index=turn,
                   query=f"当前用户问题{turn}", product_count=count, screenshot_scope="current_turn",
                   context=f"[产品已知信息] 共享条件第{turn}轮；[仅供裁判核对] 裁判资料第{turn}轮")
        for product in range(1, count + 1):
            path = tmp_path / f"p{product}-t{turn}.png"
            Image.new("RGB", (64, 96), (product * 40, turn * 40, 20)).save(path)
            row[f"screenshot{product}"] = str(path)
            row[f"context{product}"] = f"[产品{product}已知信息] 私有条件{product}-T{turn}"
            row[f"answer{product}"] = f"产品{product}原始回答第{turn}轮"
        row["query_images"] = [row["screenshot1"]] if turn == 1 else []
        rows.append(row)
    items, errors = parse_jsonl(json.dumps(rows, ensure_ascii=False), "compare")
    assert not errors
    # Pretend prior judge calls failed or assigned bad scores. The input path
    # must ignore those assessments and still send every original history turn.
    for item in items:
        item.update(rationale="DO_NOT_READ_JUDGE", turn_summary="DO_NOT_READ_JUDGE",
                    answer1_understanding_score=1, error="DO_NOT_READ_JUDGE")
    service = ConversationPreparation(items, "chat-retry-target", profile, resolve_image_limits(model, profile))
    target = items[target_turn - 1]
    bundle = service.prepare(target)
    client = FakeClient(observation_data(count))
    judge = VisualCompareJudge(client, profile, QA_CHAT_COMPARE_PROTOCOL_ID)
    output = await judge.evaluate(
        question=target["query"], product_count=count, evidence_mode="long_screenshot",
        screenshot_metas=[target[f"screenshot_meta{p}"] for p in range(1, count + 1)],
        conversation_input=bundle,
        **{f"frames{p}": target[f"frames{p}"] for p in range(1, count + 1)},
    )
    assert len(client.calls) == 1 and not client.repairs
    system, user, request = client.calls[0]
    texts = "\n".join(part["text"] for part in request["content_parts"] if part["type"] == "text")
    assert [turn["turn_index"] for turn in bundle["turns"]] == list(range(1, target_turn + 1))
    assert "DO_NOT_READ_JUDGE" not in texts
    assert "当前用户问题4" not in texts and "原始回答第4轮" not in texts
    for turn in range(1, target_turn + 1):
        assert f"当前用户问题{turn}" in texts
        assert f"共享条件第{turn}轮" not in texts and f"裁判资料第{turn}轮" not in texts
        for product in range(1, count + 1):
            assert f"产品{product}原始回答第{turn}轮" in texts
            assert f"私有条件{product}-T{turn}" in texts
    assert f"只评价第{target_turn}轮" in user
    assert "不能反推产品知道该信息" in system
    assert f'"answer{count}_personalization_score_status"' in system
    assert len(request["image_metadata"]) == 1 + count * target_turn
    assert request["image_metadata"][0]["image_role"] == "query_image"
    for product in range(1, count + 1):
        start = texts.index(f"产品{product}自身轨迹开始")
        end = texts.index(f"产品{product}轨迹结束", start)
        own_history = texts[start:end]
        assert all(f"产品{other}原始回答" not in own_history for other in range(1, count + 1) if other != product)
    assert output["standard_id"] == "qa_chat_compare"
    assert output["answer1_personalization_score"] == 5
    assert output["input_manifest_sha256"]
    assert target["history_prefix_sha256"]
