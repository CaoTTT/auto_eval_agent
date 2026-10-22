"""Chat V2 contract/normalization checks, using synthetic model output only."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from auto_eval.config import VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.base import JudgeOutputParseError
from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID, QA_CHAT_COMPARE_PROTOCOL_ID, QA_CHAT_DIMENSIONS,
    QA_CHAT_V2_COMPARE_PROTOCOL_ID, resolve_compare_protocol,
)
from auto_eval.judges.prompts import parse_json_loose, parse_json_preserving_pairs
from auto_eval.judges.qa_chat_schema import QAChatObservation
from auto_eval.judges.qa_chat_v2_schema import QAChatV2Observation, expected_output_keys
from auto_eval.judges.visual_compare_judge import VisualCompareJudge, visual_compare_result_fields
from auto_eval.judges.visual_compare_prompt_qa_chat_v2 import (
    RULES, SOURCE_ANCHORS, output_example, render_system_prompt,
)
from test_qa_chat_protocol import FakeClient
from test_multi_turn_compare import data as conversation_data, prepare as prepare_conversation


def payload(count=2):
    return json.loads(output_example(count))


def finalize(data, count=2, trusted=None):
    observation = QAChatV2Observation.model_validate(
        data, context={"product_count": count, "trusted_collection_metadata": trusted})
    return visual_compare_result_fields(observation, QA_CHAT_V2_COMPARE_PROTOCOL_ID)


def test_registry_is_separate_and_v1_defaults_unchanged():
    v1 = resolve_compare_protocol(QA_CHAT_COMPARE_PROTOCOL_ID, "1.0.0")
    v2 = resolve_compare_protocol(QA_CHAT_V2_COMPARE_PROTOCOL_ID, "1.1.0")
    assert v1.observation_model is QAChatObservation
    assert v2.observation_model is QAChatV2Observation
    assert v2.standard_version == "1.1" and v2.bundle_revision == "1.1.0"
    assert v2.result_schema_version == "qa-chat-result-2"
    assert v2.display == "问答闲聊V2·1-5分（候选）"
    assert v2.status == "experimental" and v2.score_dimensions == QA_CHAT_DIMENSIONS
    assert resolve_compare_protocol(None).id == DEFAULT_COMPARE_PROTOCOL_ID
    for profile, wrong_revision in ((v1.id, "1.1.0"), (v2.id, "1.0.0"), (v2.id, "1.2.0")):
        with pytest.raises(ValueError, match="无法恢复"):
            resolve_compare_protocol(profile, wrong_revision)


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
def test_complete_design_prompt_and_exact_keys(count, mode):
    system = render_system_prompt(product_count=count, evidence_mode=mode)
    example = payload(count)
    assert len(example) == 22 * count + 12
    assert tuple(example) == expected_output_keys(count)
    assert ("answer3_understanding_score" in example) is (count == 3)
    document = Path("docs/qa-chat-compare-v1-prompt-output-redesign.md").read_text(encoding="utf-8")
    appendix = document.split('```python\n', 1)[1].split('\n```', 1)[0]
    namespace = {}
    exec(compile(appendix, "design-appendix", "exec"), namespace)
    assert RULES == namespace["RULES"]
    assert SOURCE_ANCHORS == namespace["SOURCE_ANCHORS"]
    assert output_example(count) == namespace["output_example"](count)
    assert SOURCE_ANCHORS in system
    assert json.loads(system[system.index('{\n  "answer1_response_gate_reason"'):]) == example
    observation = QAChatV2Observation.model_validate(example, context={"product_count": count})
    assert observation.model_dump() == example
    assert json.loads(observation.model_dump_json()) == example
    assert QAChatV2Observation.model_validate_json(observation.model_dump_json(), context={"product_count": count}).model_dump() == example
    result = finalize(example, count)
    assert result["product_count"] == count and result["answer1_input_status"] is None
    for dimension in QA_CHAT_DIMENSIONS:
        assert result[f"answer1_{dimension}_score_status"] == "scored"
        assert result[f"{dimension}_reason"] == example[f"{dimension}_reason"]
        assert result[f"{dimension}_evidence"] == example[f"{dimension}_evidence"]
        assert f"answer1_{dimension}_reason" not in result
        assert f"answer1_{dimension}_evidence" not in result
        assert f"{dimension}_primary_issue" not in result
    assert not {"has_conflict", "confidence", "rationale", "overall_winner"} & result.keys()


@pytest.mark.parametrize("score", [0, 6, True, False, 4.0, "4", 3.1])
def test_strict_score_rejected_even_with_failed_gate(score):
    data = payload()
    data.update(answer1_understanding_score=score, answer1_response_gate="fail")
    with pytest.raises(ValidationError):
        finalize(data)


@pytest.mark.parametrize("key", ["answer1_response_gate", "answer2_safety_gate_reason", "style_reason", "understanding_evidence", "answer2_style_score"])
def test_required_fields_cannot_be_omitted(key):
    data = payload()
    data.pop(key)
    with pytest.raises(ValidationError, match="缺少字段"):
        finalize(data)


@pytest.mark.parametrize("extra", ["answer3_response_gate", "product_count", "answer1_reason", "answer1_input_status", "answer1_style_score_status", "has_conflict"])
def test_unexpected_model_fields_are_rejected(extra):
    data = payload()
    data.pop("answer1_style_score")
    data[extra] = "bad"
    with pytest.raises(ValidationError) as error:
        finalize(data)
    assert "缺少字段" in str(error.value) and "多余字段" in str(error.value)
    assert extra in str(error.value) and "answer1_style_score" in str(error.value)


@pytest.mark.parametrize("changes", [
    {"answer1_understanding_applicable": 1}, {"answer1_understanding_verification_status": "unknown"},
    {"answer1_understanding_score": None},
    {"answer1_understanding_applicable": None},
    {"answer1_understanding_verification_status": None},
    {"understanding_reason": " "}, {"understanding_evidence": [123]},
    {"answer1_response_gate_reason": " "}, {"answer1_response_gate": "good"},
])
def test_invalid_states_and_types_are_technical_errors(changes):
    data = payload()
    data.update(changes)
    with pytest.raises(ValidationError):
        finalize(data)


@pytest.mark.parametrize("gate", ["response", "safety"])
def test_gate_priority_preserves_other_product_scores_and_applicability(gate):
    data = payload(3)
    data[f"answer3_{gate}_gate"] = "fail"
    data.update(answer3_personalization_applicable=False, answer3_personalization_verification_status=None)
    result = finalize(data, 3)
    for dimension in QA_CHAT_DIMENSIONS:
        assert result[f"answer3_{dimension}_score_status"] == "gate_blocked"
        assert result[f"answer3_{dimension}_score"] is None
        assert result[f"answer1_{dimension}_score"] == 3
        assert result[f"{dimension}_winner"] == "tie"
        assert result[f"{dimension}_rank_groups"] == []
    assert result["answer3_personalization_applicable"] is False
    assert result["answer3_personalization_verification_status"] is None
    assert result["normalization_diagnostics"]


def test_trusted_failure_precedes_gate_and_na_with_unknown_verification():
    data = payload()
    data.update(answer1_response_gate="fail", answer1_personalization_applicable=False,
                answer1_personalization_verification_status=None)
    trusted = {"answer1": {"input_status": "failed", "source": "synthetic collector", "capture_id": "capture-1"}}
    result = finalize(data, trusted=trusted)
    assert result["answer1_response_gate"] == result["answer1_safety_gate"] == "unclear"
    assert result["answer1_personalization_score_status"] == "input_failed"
    assert result["answer1_personalization_applicable"] is False
    assert result["answer1_personalization_verification_status"] is None
    assert result["answer1_personalization_score"] is None
    assert result["answer2_personalization_score"] == 3
    assert result["trusted_collection_metadata"] == trusted
    assert result["answer1_input_status_source"] == "synthetic collector"
    assert result["personalization_reason"] == data["personalization_reason"]


@pytest.mark.parametrize("verification", ["verified", "partial", "not_required"])
def test_unclear_gate_and_unknown_collection_keep_legal_score(verification):
    data = payload()
    data.update(answer1_response_gate="unclear", answer1_understanding_verification_status=verification)
    result = finalize(data)
    assert result["answer1_input_status"] is None
    assert result["answer1_understanding_score"] == 3
    assert result["answer1_understanding_score_status"] == "scored"
    assert result["needs_human_review"]


def test_na_and_unverifiable_are_independent_and_do_not_invent_product_evidence():
    data = payload()
    data.update(answer1_personalization_applicable=False, answer2_style_verification_status="unverifiable")
    result = finalize(data)
    assert result["answer1_personalization_score_status"] == "not_applicable"
    assert result["answer1_personalization_verification_status"] == "not_required"
    assert result["answer2_personalization_score"] == 3
    assert result["answer2_style_score_status"] == "unverifiable"
    assert result["answer1_style_score"] == 3
    assert result["personalization_evidence"] == data["personalization_evidence"]
    assert "answer1_personalization_evidence" not in result


def test_unclear_evidence_ownership_is_visible_without_changing_legal_scores():
    data = payload()
    data["style_evidence"] = ["无归属的截图文字", "产品3：未启用产品"]
    result = finalize(data)
    assert result["style_evidence"] == data["style_evidence"]
    assert any("证据归属不清" in reason for reason in result["normalization_diagnostics"])
    assert result["answer1_style_score"] == 3


@pytest.mark.parametrize("raw", ['{"a":1,"a":1}', '{"a":1,"a":2}', '{"a":1,"\\u0061":2}', '{"x":{"a":1,"a":2}}', '{"a":1,"a":2,}', '{"a":1,"a":2'])
def test_duplicate_keys_are_detected_before_dict_construction(raw):
    parsed = parse_json_preserving_pairs(raw)
    assert parsed["data"] is None
    assert parsed["duplicate_keys"] and (parsed["raw_key_pairs"] or parsed["locally_repaired_key_pairs"])
    assert parsed["structural_diagnostics"]


def test_legacy_parser_and_nonduplicate_syntax_tolerance_are_unchanged():
    assert parse_json_loose('{"a":1,"a":2}') == {"a": 2}
    assert parse_json_preserving_pairs('```json\n{"a":"正文"词语"内容",}\n```')["data"] == {"a": '正文"词语"内容'}
    assert parse_json_preserving_pairs("not JSON") is None


class RawClient(FakeClient):
    def __init__(self, raw, repair=None):
        super().__init__({})
        self.raw = raw
        self.repair = repair

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return self.raw

    async def repair_json(self, raw, **kwargs):
        self.repairs.append(raw)
        if self.repair is None:
            pytest.fail("Structure errors must not trigger a model repair call")
        return self.repair


async def evaluate(client, trusted=None, **kwargs):
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    return await VisualCompareJudge(client, profile, QA_CHAT_V2_COMPARE_PROTOCOL_ID).evaluate(
        question="q", trusted_collection_metadata=trusted, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_value", [True, False])
async def test_duplicate_output_preserves_raw_pairs_and_never_calls_model_repair(same_value):
    raw = json.dumps(payload(), ensure_ascii=False)
    raw = raw[:-1] + ',"answer1_response_gate":' + ('"pass"}' if same_value else '"fail"}')
    client = RawClient(raw, repair=json.dumps(payload()))
    with pytest.raises(JudgeOutputParseError) as error:
        await evaluate(client)
    assert error.value.raw_output == raw and error.value.raw_model_output == raw
    assert len([pair for pair in error.value.raw_key_pairs if pair[0] == "answer1_response_gate"]) == 2
    assert error.value.structural_diagnostics and error.value.duplicate_keys
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["[]", "[1,2]", "4", '"text"', '"{}"', "null", "true", "false"])
async def test_valid_nonobject_json_is_structure_error_without_model_repair(raw):
    parsed = parse_json_preserving_pairs(raw)
    assert parsed is not None and parsed["top_level_type"] != "object"
    assert parsed["structural_diagnostics"]
    client = RawClient(raw)
    with pytest.raises(JudgeOutputParseError, match="必须为JSON对象") as error:
        await evaluate(client)
    assert error.value.raw_model_output == raw
    assert error.value.structural_diagnostics
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
async def test_complete_result_inside_json_array_is_not_silently_unwrapped():
    raw = "[" + json.dumps(payload()) + "]"
    parsed = parse_json_preserving_pairs(raw)
    assert parsed["top_level_type"] == "array"
    assert parsed["data"] == [payload()]
    assert parsed["raw_key_pairs"]
    client = RawClient(raw)
    with pytest.raises(JudgeOutputParseError, match="必须为JSON对象") as error:
        await evaluate(client)
    assert error.value.raw_key_pairs == parsed["raw_key_pairs"]
    assert not client.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [
    lambda obj: "[" + obj + ",]",
    lambda obj: "[" + obj,
    lambda obj: "[" + obj[:-1] + ",}]",
    lambda obj: "[" + obj[:-1] + ",},]",
    lambda obj: '"' + obj + '"',
    lambda obj: '"' + obj,
    lambda obj: "'" + obj + "'",
])
async def test_malformed_array_and_string_wrappers_cannot_be_stripped_to_objects(wrapper):
    raw = wrapper(json.dumps(payload()))
    parsed = parse_json_preserving_pairs(raw)
    assert parsed["top_level_type"] in ("array", "string")
    assert parsed["data"] is None
    assert parsed["candidate"] == raw
    assert not parsed["locally_repaired"]
    client = RawClient(raw)
    with pytest.raises(JudgeOutputParseError, match="必须为JSON对象") as error:
        await evaluate(client)
    assert error.value.raw_model_output == raw
    assert error.value.structural_diagnostics
    assert len(client.calls) == 1 and not client.repairs


def test_nonjson_prose_does_not_silently_extract_an_inner_result_object():
    raw = "前置说明 " + json.dumps(payload()) + " 后置说明"
    assert parse_json_preserving_pairs(raw) is None


@pytest.mark.asyncio
async def test_syntax_repair_cannot_return_valid_nonobject_json():
    client = RawClient(json.dumps(payload())[:-1], repair="null")
    with pytest.raises(JudgeOutputParseError, match="修复输出必须为JSON对象"):
        await evaluate(client)
    assert len(client.repairs) == 1


@pytest.mark.asyncio
async def test_local_trailing_comma_repair_preserves_literal_evidence_and_pair_origins():
    data = payload()
    literal = '原话包含 ,}、,]、, }、, ] 和转义引号 "内容,}"、反斜线 \\、\\"'
    data["style_reason"] = "产品1：" + literal + "；产品2：" + literal
    data["style_evidence"] = ["产品1 T01：" + literal, "产品2 T01：" + literal]
    raw = json.dumps(data, ensure_ascii=False)[:-1] + ",}"
    parsed = parse_json_preserving_pairs(raw)
    assert parsed["locally_repaired"]
    assert parsed["raw_key_pairs"] == []
    assert len(parsed["locally_repaired_key_pairs"]) == 56
    assert parsed["data"]["style_reason"] == data["style_reason"]
    assert parsed["data"]["style_evidence"] == data["style_evidence"]
    client = RawClient(raw)
    result = await evaluate(client)
    assert result["style_reason"] == data["style_reason"]
    assert result["style_evidence"] == data["style_evidence"]
    assert result["raw_model_output"] == raw
    assert result["raw_key_pairs"] == result["original_raw_key_pairs"] == []
    assert len(result["locally_repaired_key_pairs"]) == 56
    assert result["repair_key_pairs"] == result["repair_locally_repaired_key_pairs"] == []
    assert len(client.calls) == 1 and not client.repairs


@pytest.mark.asyncio
async def test_llm_repair_and_subsequent_local_repair_have_distinct_pair_records():
    repaired = json.dumps(payload())[:-1] + ",}"
    client = RawClient(json.dumps(payload())[:-1], repair=repaired)
    result = await evaluate(client)
    assert result["raw_key_pairs"] == result["original_raw_key_pairs"]
    assert len(result["raw_key_pairs"]) == 56
    assert result["locally_repaired_key_pairs"] == result["repair_key_pairs"] == []
    assert len(result["repair_locally_repaired_key_pairs"]) == 56
    assert len(client.repairs) == 1


@pytest.mark.asyncio
async def test_missing_wrong_keys_report_together_without_structure_repair():
    data = payload()
    data.pop("style_reason")
    data["answer1_reason"] = "wrong alias"
    client = RawClient(json.dumps(data))
    with pytest.raises(JudgeOutputParseError) as error:
        await evaluate(client)
    assert "style_reason" in str(error.value) and "answer1_reason" in str(error.value)
    assert error.value.raw_key_pairs and error.value.structural_diagnostics
    assert not client.repairs


@pytest.mark.asyncio
async def test_duplicate_diagnostics_also_report_missing_and_unexpected_keys():
    data = payload()
    data.pop("style_reason")
    raw = json.dumps(data)[:-1] + ',"answer1_reason":"wrong alias","answer1_response_gate":"fail"}'
    client = RawClient(raw)
    with pytest.raises(JudgeOutputParseError) as error:
        await evaluate(client)
    diagnostics = "；".join(error.value.structural_diagnostics)
    assert "重复键" in diagnostics and "缺少字段" in diagnostics and "多余字段" in diagnostics
    assert "style_reason" in diagnostics and "answer1_reason" in diagnostics
    assert error.value.original_raw_key_pairs == error.value.raw_key_pairs
    assert error.value.repair_key_pairs == []
    assert not client.repairs


@pytest.mark.asyncio
async def test_one_syntax_repair_still_runs_strict_duplicate_check():
    original = json.dumps(payload())[:-1]
    client = RawClient(original, repair=json.dumps(payload()))
    result = await evaluate(client)
    assert len(client.calls) == len(client.repairs) == 1
    assert result["raw_model_output"] == original
    assert result["repaired_model_output"] and len(result["repair_key_pairs"]) == 56
    assert result["raw_key_pairs"] == result["original_raw_key_pairs"]
    assert len(result["raw_key_pairs"]) == 56
    duplicated = json.dumps(payload())[:-1] + ',"answer1_response_gate":"pass"}'
    client = RawClient(original, repair=duplicated)
    with pytest.raises(JudgeOutputParseError, match="重复键") as error:
        await evaluate(client)
    assert len(client.repairs) == 1
    assert len(error.value.raw_key_pairs) == 56
    assert len(error.value.repair_key_pairs) == 57


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["answer1_response_gate", "answer1_understanding_score", "answer1_personalization_applicable", "answer1_style_verification_status", "style_reason"])
async def test_syntax_error_cannot_hide_missing_judgments_or_trigger_semantic_repair(missing):
    data = payload()
    data.pop(missing)
    client = RawClient(json.dumps(data)[:-1], repair=json.dumps(payload()))
    with pytest.raises(JudgeOutputParseError, match="禁止语义补判") as error:
        await evaluate(client)
    assert missing in str(error.value)
    assert len(error.value.raw_key_pairs) == 55
    assert not client.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"answer1_input_status": "failed"}, {"answer1_understanding_score": 0},
    {"answer1_understanding_score": None}, {"answer1_style_applicable": None},
    {"answer1_response_gate": "unknown"},
])
async def test_syntax_error_with_known_structure_failure_is_not_sent_for_model_repair(changes):
    data = payload()
    data.update(changes)
    client = RawClient(json.dumps(data)[:-1], repair=json.dumps(payload()))
    with pytest.raises(JudgeOutputParseError, match="禁止语义补判"):
        await evaluate(client)
    assert not client.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("original", ["invalid JSON", "not JSON", '{"answer1_response_gate":', '{"answer1_response_gate":"pa'])
async def test_unrecoverable_original_judgments_cannot_be_created_by_repair(original):
    client = RawClient(original, repair=json.dumps(payload()))
    with pytest.raises(JudgeOutputParseError, match="禁止语义补判"):
        await evaluate(client)
    assert not client.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("key,replacement", [
    ("answer1_response_gate", "fail"), ("answer1_understanding_score", 5),
    ("answer1_personalization_applicable", False),
    ("answer1_style_verification_status", "unverifiable"),
    ("style_reason", "产品1：修复器新增的理由；产品2：修复器新增的理由"),
])
async def test_syntax_repair_cannot_change_any_original_decision_or_reason(key, replacement):
    original = payload()
    repaired = {**original, key: replacement}
    client = RawClient(json.dumps(original)[:-1], repair=json.dumps(repaired))
    with pytest.raises(JudgeOutputParseError, match="改变了原始判定") as error:
        await evaluate(client)
    assert key in "；".join(error.value.structural_diagnostics)
    assert len(client.repairs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [lambda obj: obj[:-1], lambda obj: "前置说明 " + obj + " 后置说明"])
async def test_genuine_syntax_only_repair_retains_all_original_values(wrapper):
    original = payload()
    client = RawClient(wrapper(json.dumps(original)), repair=json.dumps(original, sort_keys=True))
    result = await evaluate(client)
    assert len(client.calls) == len(client.repairs) == 1
    assert dict(result["raw_key_pairs"]) == original
    assert dict(result["repair_key_pairs"]) == original
    assert result["style_reason"] == original["style_reason"]
    assert result["answer1_understanding_score"] == original["answer1_understanding_score"]


@pytest.mark.asyncio
async def test_program_collection_conclusions_bind_request_fingerprint_and_capture():
    from datetime import datetime, timezone
    time = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
    raw = json.dumps(payload())
    unknown, partial = RawClient(raw), RawClient(raw)
    audits = []
    first = await evaluate(unknown, evaluation_time=time, evidence_callback=audits.append)
    second = await evaluate(partial, trusted={"answer1": {"input_status": "partial", "source": "capture"}}, evaluation_time=time, evidence_callback=audits.append)
    assert first["answer1_input_status"] is None
    assert second["answer1_input_status"] == "partial"
    assert first["answer1_understanding_score"] == second["answer1_understanding_score"] == 3
    assert first["input_manifest_sha256"] != second["input_manifest_sha256"]
    assert audits[-1]["trusted_collection_metadata"] == second["trusted_collection_metadata"]
    assert audits[-1]["input_manifest_sha256"] == second["input_manifest_sha256"]
    assert first["raw_key_pairs"] and len(first["raw_key_pairs"]) == 56
    assert not unknown.repairs and not partial.repairs


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("mode", ["video_frames", "long_screenshot"])
async def test_v2_media_single_call_keeps_existing_image_path(tmp_path, count, mode):
    from PIL import Image
    from auto_eval.long_screenshot import prepare_long_screenshot

    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    client = RawClient(json.dumps(payload(count)))
    kwargs, metadata = {}, []
    for product in range(1, count + 1):
        path = tmp_path / f"p{product}.png"
        Image.new("RGB", (64, 96), (product * 30, 30, 90)).save(path)
        if mode == "long_screenshot":
            meta = prepare_long_screenshot(path, tmp_path / str(product), profile.long_screenshot)
            metadata.append(meta)
            kwargs[f"frames{product}"] = [part["path"] for part in meta["slices"]]
        else:
            kwargs[f"frames{product}"] = [str(path)]
    if mode == "long_screenshot":
        kwargs["screenshot_metas"] = metadata
    result = await VisualCompareJudge(client, profile, QA_CHAT_V2_COMPARE_PROTOCOL_ID).evaluate(
        question="q", product_count=count, evidence_mode=mode, **kwargs)
    assert len(client.calls) == 1 and not client.repairs
    assert result["product_count"] == count
    assert result[f"answer{count}_style_score"] == 3
    assert len(result["raw_key_pairs"]) == 22 * count + 12
    if mode == "long_screenshot":
        assert len(client.calls[0][2]["image_metadata"]) == count
        assert "user_images" not in client.calls[0][2]
    else:
        assert len(client.calls[0][2]["user_images"]) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_v2_original_multiturn_inputs_and_trusted_binding(conversation_data, count):
    items, profile, _ = conversation_data
    if count == 3:
        for item in items:
            item.update(product_count=3, screenshot3=item["screenshot1"].replace("p1-", "p3-"), answer3="产品3原始回答", context3="产品3背景")
    prepared, service = prepare_conversation(conversation_data)
    target = prepared[1]
    bundle = service.prepare(target)
    client = RawClient(json.dumps(payload(count)))
    kwargs = {f"frames{product}": target[f"frames{product}"] for product in range(1, count + 1)}
    kwargs.update(screenshot_metas=[target[f"screenshot_meta{product}"] for product in range(1, count + 1)],
                  query_image_meta=target.get("query_image_meta"))
    judge = VisualCompareJudge(client, profile, QA_CHAT_V2_COMPARE_PROTOCOL_ID)
    result = await judge.evaluate(question=target["query"], product_count=count, evidence_mode="long_screenshot", conversation_input=bundle, **kwargs)
    text = "\n".join(part["text"] for part in client.calls[0][2]["content_parts"] if part["type"] == "text")
    assert "问题1" in text and "问题2" in text and "问题3" not in text
    assert [turn["turn_index"] for turn in bundle["turns"]] == [1, 2]
    assert result["answer1_input_status"] is None
    assert result["answer1_understanding_score_status"] == "scored"
    assert result["standard_version"] == "1.1"
    assert len(client.calls[0][2]["image_metadata"]) == 1 + 2 * count
    second = await judge.evaluate(question=target["query"], product_count=count, evidence_mode="long_screenshot", conversation_input=bundle,
                                  trusted_collection_metadata={"answer1": {"input_status": "partial", "source": "synthetic collector"}}, **kwargs)
    assert result["input_manifest_sha256"] != second["input_manifest_sha256"]
    assert second["answer1_understanding_score"] == result["answer1_understanding_score"]
