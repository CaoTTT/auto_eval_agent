"""Candidate knowledge QA contract boundaries, with no model or network calls."""
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from auto_eval.judges.knowledge_qa_schema import (
    KNOWLEDGE_QA_ALLOWED_SCORES, KNOWLEDGE_QA_DIMENSIONS,
    KnowledgeQAObservation, expected_output_keys, parse_output, validate_output,
)


def payload(count=2):
    data = {}
    for dimension in KNOWLEDGE_QA_DIMENSIONS:
        data[dimension + "_evidence"] = ["产品1：当前轮截图中的可见内容。"]
        data[dimension + "_reason"] = "；".join(
            f"产品{product}：按当前任务要求独立评分。"
            for product in range(1, count + 1)
        )
        for product in range(1, count + 1):
            prefix = f"answer{product}_{dimension}"
            data[prefix + "_status"] = "scored"
            data[prefix + "_score"] = 2
    return data


@pytest.mark.parametrize("count,key_count", [(2, 24), (3, 32)])
def test_exact_active_product_contract_round_trips(count, key_count):
    data = payload(count)
    original = deepcopy(data)
    observation = validate_output(data, count)
    assert observation.product_count == count
    assert tuple(observation.model_dump()) == expected_output_keys(count)
    assert len(observation.model_dump()) == key_count
    assert observation.model_dump() == json.loads(observation.model_dump_json()) == original
    assert data == original
    if count == 2:
        assert not any(key.startswith("answer3_") for key in observation.model_dump())


@pytest.mark.parametrize("count", [None, True, False, "2", 2.0, 0, 1, 4])
def test_product_count_must_be_a_supported_strict_integer(count):
    with pytest.raises(ValueError):
        expected_output_keys(count)
    with pytest.raises(ValidationError):
        validate_output(payload(), count)


def test_validation_requires_request_product_count_context():
    with pytest.raises(ValidationError, match="product_count"):
        KnowledgeQAObservation.model_validate(payload())


@pytest.mark.parametrize("count", [2, 3])
def test_each_required_key_is_enforced(count):
    for key in expected_output_keys(count):
        data = payload(count)
        data.pop(key)
        with pytest.raises(ValidationError, match="缺少字段"):
            validate_output(data, count)


@pytest.mark.parametrize("extra", [
    "answer3_accuracy_score", "answer3_accuracy_status", "answer1_response_gate",
    "answer1_safety_gate", "accuracy_winner", "total_score", "product_count",
    "evaluation_profile", "answer1_input_status", "accuracy_applicable",
    "answer1_accuracy_verification_status",
])
def test_legacy_fields_metadata_and_inactive_products_are_rejected(extra):
    with pytest.raises(ValidationError, match="多余字段"):
        validate_output({**payload(), extra: None}, 2)


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("dimension", KNOWLEDGE_QA_DIMENSIONS)
def test_only_declared_score_values_are_accepted(count, dimension):
    for score in KNOWLEDGE_QA_ALLOWED_SCORES[dimension]:
        data = payload(count)
        key = f"answer{count}_{dimension}_score"
        data[key] = score
        result = validate_output(data, count).model_dump()
        assert result[key] == score
        assert type(result[key]) is type(score)


@pytest.mark.parametrize("dimension", KNOWLEDGE_QA_DIMENSIONS)
@pytest.mark.parametrize("score", [True, False, "0", "1.5", "2", -1, 3, 0.5, 1.25,
                                    0.0, 1.0, 2.0, float("nan"), float("inf"), -float("inf")])
def test_invalid_scores_are_not_coerced_even_when_unscored(dimension, score):
    data = payload()
    data[f"answer1_{dimension}_score"] = score
    with pytest.raises(ValidationError):
        validate_output(data, 2)
    data[f"answer1_{dimension}_status"] = "insufficient_evidence"
    with pytest.raises(ValidationError):
        validate_output(data, 2)


@pytest.mark.parametrize("dimension", [d for d in KNOWLEDGE_QA_DIMENSIONS if d != "accuracy"])
def test_half_mark_is_accuracy_only(dimension):
    data = payload()
    data[f"answer1_{dimension}_score"] = 1.5
    with pytest.raises(ValidationError):
        validate_output(data, 2)


@pytest.mark.parametrize("dimension", KNOWLEDGE_QA_DIMENSIONS)
@pytest.mark.parametrize("status", ["insufficient_evidence", "unverifiable"])
def test_null_states_are_preserved_and_do_not_block_other_dimensions(dimension, status):
    data = payload(3)
    prefix = f"answer2_{dimension}"
    data[prefix + "_status"] = status
    data[prefix + "_score"] = None
    result = validate_output(data, 3).model_dump()
    assert result == data
    assert result[prefix + "_score"] is None
    for other in KNOWLEDGE_QA_DIMENSIONS:
        assert result[f"answer1_{other}_score"] == result[f"answer3_{other}_score"] == 2


def test_media_not_applicable_is_explicit_and_never_converted_to_zero():
    data = payload()
    data.update(answer1_multimedia_value_status="not_applicable",
                answer1_multimedia_value_score=None)
    assert validate_output(data, 2).model_dump() == data


@pytest.mark.parametrize("dimension", [d for d in KNOWLEDGE_QA_DIMENSIONS if d != "multimedia_value"])
def test_nonmedia_dimensions_cannot_claim_not_applicable(dimension):
    data = payload()
    data[f"answer1_{dimension}_status"] = "not_applicable"
    data[f"answer1_{dimension}_score"] = None
    with pytest.raises(ValidationError, match="仅富媒体增益"):
        validate_output(data, 2)


@pytest.mark.parametrize("dimension", KNOWLEDGE_QA_DIMENSIONS)
def test_scored_requires_a_number_and_unscored_forbids_any_number(dimension):
    data = payload()
    prefix = f"answer1_{dimension}"
    data[prefix + "_score"] = None
    with pytest.raises(ValidationError, match="仅scored"):
        validate_output(data, 2)
    for status in ("not_applicable", "insufficient_evidence", "unverifiable"):
        data[prefix + "_status"] = status
        data[prefix + "_score"] = 0
        with pytest.raises(ValidationError, match="仅scored"):
            validate_output(data, 2)


@pytest.mark.parametrize("status", [None, "", "pass", "fail", "unclear", "gate_blocked", "unlabeled", True, 1])
def test_status_enum_is_independent_of_legacy_gate_and_execution_states(status):
    data = payload()
    data["answer1_accuracy_status"] = status
    with pytest.raises(ValidationError):
        validate_output(data, 2)


def test_active_third_product_status_cannot_be_null_even_with_a_null_score():
    data = payload(3)
    data.update(answer3_accuracy_status=None, answer3_accuracy_score=None)
    with pytest.raises(ValidationError, match="活动产品"):
        validate_output(data, 3)


@pytest.mark.parametrize("reason", [None, "", " \n\t", 1, True, []])
def test_reasons_are_required_nonblank_strings(reason):
    data = payload()
    data["accuracy_reason"] = reason
    with pytest.raises(ValidationError):
        validate_output(data, 2)


@pytest.mark.parametrize("evidence", [None, "产品1：文本", [1], [True], [None], {}])
def test_evidence_requires_a_list_of_strict_strings(evidence):
    data = payload()
    data["accuracy_evidence"] = evidence
    with pytest.raises(ValidationError):
        validate_output(data, 2)


def test_absent_visible_evidence_can_remain_an_empty_list_with_explanation():
    data = payload()
    data.update(accuracy_evidence=[], accuracy_reason="产品1：缺少必要截图；产品2：无法核验外部事实。",
                answer1_accuracy_status="insufficient_evidence", answer1_accuracy_score=None,
                answer2_accuracy_status="unverifiable", answer2_accuracy_score=None)
    assert validate_output(data, 2).model_dump() == data


def test_accuracy_zero_does_not_apply_a_hidden_gate_to_other_scores():
    data = payload()
    data["answer1_accuracy_score"] = 0
    result = validate_output(data, 2).model_dump()
    assert result == data
    for dimension in KNOWLEDGE_QA_DIMENSIONS[1:]:
        assert result[f"answer1_{dimension}_status"] == "scored"
        assert result[f"answer1_{dimension}_score"] == 2


@pytest.mark.parametrize("data", [None, [], "{}", 1, True])
def test_top_level_must_be_an_object(data):
    with pytest.raises(ValidationError, match="JSON对象"):
        validate_output(data, 2)


@pytest.mark.parametrize("count", [2, 3])
def test_strict_json_parser_preserves_half_marks_nulls_and_text(count):
    data = payload(count)
    data.update(answer1_accuracy_score=1.5,
                answer2_multimedia_value_status="not_applicable",
                answer2_multimedia_value_score=None,
                clarity_and_structure_reason="产品1：原文含数字1.5及文字NaN；产品2：独立评分。")
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False)
    assert parse_output(raw, count).model_dump() == data
    assert parse_output(" \n" + raw + "\t", count).model_dump() == data
    assert type(parse_output(raw, count).answer1_accuracy_score) is float


def test_strict_json_parser_rejects_duplicate_root_keys_without_last_value_wins():
    raw = json.dumps(payload())
    duplicate = '{"answer1_accuracy_score": 0, ' + raw[1:]
    with pytest.raises(ValueError, match="重复键：answer1_accuracy_score"):
        parse_output(duplicate, 2)


def test_strict_json_parser_rejects_identical_duplicate_keys_too():
    raw = json.dumps(payload())
    duplicate = '{"answer1_accuracy_score": 2, ' + raw[1:]
    with pytest.raises(ValueError, match="重复键"):
        parse_output(duplicate, 2)


@pytest.mark.parametrize("nested", [
    '{"x": 1, "x": 2}', '[{"x": 1, "x": 1}]',
])
def test_strict_json_parser_rejects_nested_duplicates_before_schema_validation(nested):
    raw = json.dumps(payload())
    raw = '{"unexpected_nested": ' + nested + ', ' + raw[1:]
    with pytest.raises(ValueError, match="重复键：x"):
        parse_output(raw, 2)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_strict_json_parser_rejects_nonstandard_numeric_constants(constant):
    raw = json.dumps(payload()).replace('"answer1_accuracy_score": 2',
                                        '"answer1_accuracy_score": ' + constant)
    with pytest.raises(ValueError, match="非法JSON常量"):
        parse_output(raw, 2)


def test_strict_json_parser_rejects_overflow_to_nonfinite_numbers():
    raw = json.dumps(payload()).replace('"answer1_accuracy_score": 2',
                                        '"answer1_accuracy_score": 1e999')
    with pytest.raises(ValueError, match="数值必须有限"):
        parse_output(raw, 2)


@pytest.mark.parametrize("raw", ["null", "[]", '"text"', "1", "1.5", "true"])
def test_strict_json_parser_rejects_nonobject_roots(raw):
    with pytest.raises(ValidationError, match="JSON对象"):
        parse_output(raw, 2)


@pytest.mark.parametrize("raw", [None, b"{}", "", " \n\t"])
def test_strict_json_parser_rejects_empty_or_nontext_output(raw):
    with pytest.raises(ValueError, match="非空JSON文本"):
        parse_output(raw, 2)


@pytest.mark.parametrize("wrapper", [
    "```json\n{}\n```", "说明：{}", "{}\n评估完成", "{} {}",
])
def test_strict_json_parser_rejects_fences_commentary_and_multiple_objects(wrapper):
    raw = wrapper.replace("{}", json.dumps(payload()), 1)
    with pytest.raises(ValueError, match="完整合法JSON文本"):
        parse_output(raw, 2)


def test_strict_json_parser_does_not_repair_missing_fields_or_trailing_commas():
    data = payload()
    data.pop("answer1_accuracy_score")
    with pytest.raises(ValidationError, match="缺少字段"):
        parse_output(json.dumps(data), 2)
    with pytest.raises(ValueError, match="完整合法JSON文本"):
        parse_output(json.dumps(payload())[:-1] + ",}", 2)
