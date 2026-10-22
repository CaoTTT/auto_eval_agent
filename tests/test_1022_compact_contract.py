"""Behavioral checks for the compact 1022 wire contracts."""
import json

import pytest

from auto_eval.judges.compare_1022_contract import (
    DIMENSIONS_1022, Compare1022DualOutput, Compare1022TripleOutput,
    OutputContractError, example_1022_output, is_unscored_reason_1022,
    output_model_1022, output_schema_1022, parse_1022_dual_output,
    parse_1022_output, parse_1022_triple_output, product_reason_1022,
    validate_1022_output,
)


def encoded(data):
    return json.dumps(data, ensure_ascii=False)


def gate_only(count, *, gate="response", verdict="fail"):
    source = example_1022_output(count)
    data = {key: value for key, value in source.items()
            if key.startswith("answer") and not key.endswith("_score")}
    for n in range(1, count + 1):
        data[f"answer{n}_{gate}_gate"] = verdict
    return data


@pytest.mark.parametrize("count,expected", [(2, 36), (3, 47)])
def test_complete_output_and_provider_schema_use_only_actual_products(count, expected):
    raw = example_1022_output(count)
    result = parse_1022_output(encoded(raw), count)
    schema = output_schema_1022(count)
    assert len(raw) == len(result) == len(schema["properties"]) == expected
    assert result == raw
    assert list(schema["properties"]) == list(raw)
    assert set(schema["required"]) == set(raw)
    assert schema["additionalProperties"] is False
    keys = list(raw)
    assert keys[:count] == [f"answer{n}_input_status" for n in range(1, count + 1)]
    for n in range(1, count + 1):
        for gate in ("response", "safety"):
            key = f"answer{n}_{gate}_gate_reason"
            assert keys[keys.index(key) + 1] == f"answer{n}_{gate}_gate"
    for dimension in DIMENSIONS_1022:
        start = keys.index(f"{dimension}_applicable")
        assert keys[start:start + count + 2] == [
            f"{dimension}_applicable", f"{dimension}_reason",
            *[f"answer{n}_{dimension}_score" for n in range(1, count + 1)],
        ]
    assert keys[-2:] == ["conflict_reason", "content_conflict"]
    assert any(key.startswith("answer3_") for key in raw) == (count == 3)
    assert "accuracy" not in DIMENSIONS_1022


def test_dual_and_triple_have_independent_models_and_entrypoints():
    assert output_model_1022(2) is Compare1022DualOutput
    assert output_model_1022(3) is Compare1022TripleOutput
    assert Compare1022DualOutput is not Compare1022TripleOutput
    assert parse_1022_dual_output(encoded(example_1022_output(2)))
    assert parse_1022_triple_output(encoded(example_1022_output(3)))
    for parser, wrong_count in [(parse_1022_dual_output, 3), (parse_1022_triple_output, 2)]:
        with pytest.raises(OutputContractError):
            parser(encoded(example_1022_output(wrong_count)))


@pytest.mark.parametrize("count", [0, 1, 4, 2.0, True])
def test_unsupported_product_count_is_rejected(count):
    with pytest.raises(ValueError):
        output_model_1022(count)


@pytest.mark.parametrize("field,value", [
    ("accuracy_applicable", False), ("accuracy_reason", "未评估"),
    ("answer1_accuracy_score", None), ("understanding_verification_status", "verified"),
    ("understanding_evidence", "证据"), ("understanding_primary_issue", "问题"),
    ("evidence", []), ("confidence", "high"), ("needs_human_review", False),
    ("review_reasons", []), ("rationale", "解释"), ("answer3_input_status", None),
    ("standard_id", "qa_competitor_compare"), ("standard_version", "0.2-simplified-1022"),
    ("product_count", 2),
])
def test_removed_or_app_managed_fields_are_not_accepted(field, value):
    raw = example_1022_output(2)
    raw[field] = value
    with pytest.raises(OutputContractError) as caught:
        validate_1022_output(raw, 2)
    assert caught.value.category == "schema"


@pytest.mark.parametrize("key", [
    "answer1_input_status", "answer1_response_gate_reason",
    "understanding_applicable", "understanding_reason", "conflict_reason",
])
def test_each_field_must_stay_in_its_ordered_group(key):
    raw = example_1022_output(2)
    raw[key] = raw.pop(key)
    with pytest.raises(OutputContractError, match="字段顺序"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("count", [2, 3])
def test_reasons_first_layout_belongs_only_to_frozen_r022(count):
    from auto_eval.judges.compare_1022_contract_r022 import (
        example_1022_output as old_example, validate_1022_output as old_validate,
    )
    previous = old_example(count)
    assert old_validate(previous, count) == previous
    with pytest.raises(OutputContractError, match="字段顺序"):
        validate_1022_output(previous, count)
    with pytest.raises(OutputContractError):
        old_validate(example_1022_output(count), count)


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("gate", ["response", "safety"])
@pytest.mark.parametrize("verdict", ["fail", "unclear"])
@pytest.mark.parametrize("supplied_score", ["omit", "bad", "numeric"])
def test_either_gate_blocks_product_and_other_products_remain_scored(
    count, gate, verdict, supplied_score,
):
    raw = example_1022_output(count)
    raw[f"answer1_{gate}_gate"] = verdict
    for dimension in DIMENSIONS_1022:
        key = f"answer1_{dimension}_score"
        if supplied_score == "omit":
            del raw[key]
        else:
            raw[key] = "invalid downstream value" if supplied_score == "bad" else 4
        if raw[f"{dimension}_applicable"]:
            raw[f"{dimension}_reason"] = "；".join(
                f"产品{n}：核心问候完整、简短清晰，直接满足需求。"
                for n in range(2, count + 1)
            )
    result = validate_1022_output(raw, count)
    assert all(result[f"answer1_{dimension}_score"] is None for dimension in DIMENSIONS_1022)
    assert result["answer2_understanding_score"] == 5
    assert is_unscored_reason_1022(product_reason_1022(result["understanding_reason"], 1))
    if count == 2:
        assert result["content_conflict"] == "unclear"
    else:
        assert result["content_conflict"] == "no"


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("gate", ["response", "safety"])
@pytest.mark.parametrize("verdict", ["fail", "unclear"])
def test_all_products_blocked_permit_gate_only_json(count, gate, verdict):
    result = validate_1022_output(gate_only(count, gate=gate, verdict=verdict), count)
    for dimension in DIMENSIONS_1022:
        assert result[f"{dimension}_applicable"] is None
        for n in range(1, count + 1):
            assert result[f"answer{n}_{dimension}_score"] is None
            assert is_unscored_reason_1022(product_reason_1022(result[f"{dimension}_reason"], n))
    assert result["content_conflict"] == "unclear"
    assert result["conflict_reason"]
    assert len(result) == (36 if count == 2 else 47)


@pytest.mark.parametrize("count", [2, 3])
def test_failed_inputs_require_unclear_gates_and_fill_downstream(count):
    raw = gate_only(count, verdict="unclear")
    for n in range(1, count + 1):
        raw[f"answer{n}_input_status"] = "failed"
        raw[f"answer{n}_safety_gate"] = "unclear"
    result = validate_1022_output(raw, count)
    assert all(result[f"answer{n}_understanding_score"] is None for n in range(1, count + 1))
    assert "输入失败" in result["understanding_reason"]
    raw["answer1_safety_gate"] = "pass"
    with pytest.raises(OutputContractError, match="两个Gate必须"):
        validate_1022_output(raw, count)


@pytest.mark.parametrize("missing", [
    "answer2_understanding_score", "answer2_service_closure_score",
    "understanding_applicable", "understanding_reason",
])
def test_one_blocked_product_does_not_allow_missing_active_product_fields(missing):
    raw = example_1022_output(2)
    raw["answer1_safety_gate"] = "fail"
    del raw[missing]
    with pytest.raises(OutputContractError):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("missing", [
    "answer1_response_gate_reason", "answer1_safety_gate_reason",
    "answer1_input_status", "answer1_response_gate", "answer1_safety_gate",
])
def test_gate_shortcut_still_requires_input_and_gate_explanations(missing):
    raw = gate_only(2)
    del raw[missing]
    with pytest.raises(OutputContractError) as caught:
        validate_1022_output(raw, 2)
    assert caught.value.category == "schema"


def test_null_score_needs_explicit_product_specific_unscored_explanation():
    raw = example_1022_output(2)
    raw["answer1_understanding_score"] = None
    with pytest.raises(OutputContractError, match="空分须用"):
        validate_1022_output(raw, 2)
    raw["understanding_reason"] = (
        "产品1：未评分（证据不足）：截图未展示核心问句对应的回答部分；"
        "产品2：可见回答完整回应问候目标。"
    )
    result = validate_1022_output(raw, 2)
    assert result["answer1_understanding_score"] is None
    assert result["answer2_understanding_score"] == 5
    assert product_reason_1022(result["understanding_reason"], 1).startswith("未评分（证据不足）")
    raw["answer1_understanding_score"] = 3
    with pytest.raises(OutputContractError, match="有分数却声明未评分"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("reason", [
    "", "未评分", "未评分（）：看不到", "未评分（证据不足）：",
    "未评分（...）：...", "未评分（证据不足）：；", "证据不足，无法评分",
])
def test_empty_or_ambiguous_unscored_explanations_are_rejected(reason):
    assert not is_unscored_reason_1022(reason)


def test_not_applicable_requires_query_level_explanation_and_null_scores():
    raw = example_1022_output(2)
    raw["answer1_service_closure_score"] = 3
    with pytest.raises(OutputContractError, match="不适用时必须为null"):
        validate_1022_output(raw, 2)
    raw["answer1_service_closure_score"] = None
    raw["service_closure_reason"] = "产品1：没有信息；产品2：没有信息"
    with pytest.raises(OutputContractError, match="查询级原因"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("reason", [
    "产品2：清楚；产品1：清楚",
    "产品1：清楚；产品1：清楚；产品2：清楚",
    "产品1：清楚；产品2：清楚；产品3：清楚",
    "产品1：清楚；产品2：清楚；产品4：清楚",
    "产品0：清楚；产品1：清楚；产品2：清楚",
    "总体不错；产品1：清楚；产品2：清楚",
    "产品1：；产品2：清楚",
    "产品1：清楚",
])
def test_applicable_reason_sections_must_cover_actual_active_products_in_order(reason):
    raw = example_1022_output(2)
    raw["understanding_reason"] = reason
    with pytest.raises(OutputContractError):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("score", [0, 6, True, "5", 4.5])
def test_scored_products_require_strict_integer_one_to_five(score):
    raw = example_1022_output(2)
    raw["answer1_understanding_score"] = score
    with pytest.raises(OutputContractError) as caught:
        validate_1022_output(raw, 2)
    assert caught.value.category == "schema"


def test_reason_cannot_claim_a_different_numeric_grade():
    raw = example_1022_output(2)
    raw["understanding_reason"] = "产品1：应给3分，因为漏掉核心要求；产品2：完整满足要求"
    with pytest.raises(OutputContractError, match="数值评分"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("text", [
    '{"answer1_response_gate":"pass","answer1_response_gate":"fail"}',
    '{"key": NaN}', '{"key": Infinity}', '{"key": -Infinity}',
    '{"key":', '[]', '{}{}', '前言 {}', chr(96) * 3 + 'json\n{}\n' + chr(96) * 3,
])
def test_duplicate_truncated_nonfinite_and_nonobject_json_is_rejected(text):
    with pytest.raises(OutputContractError) as caught:
        parse_1022_dual_output(text)
    assert caught.value.category == "json_syntax"


def test_shared_conflict_can_be_omitted_only_when_fewer_than_two_products_are_active():
    raw = example_1022_output(2)
    del raw["conflict_reason"]
    del raw["content_conflict"]
    with pytest.raises(OutputContractError, match="内容冲突判定"):
        validate_1022_output(raw, 2)
    raw["answer1_response_gate"] = "fail"
    result = validate_1022_output(raw, 2)
    assert result["content_conflict"] == "unclear"


@pytest.mark.parametrize("value", [None, "invalid ignored value", {"unexpected": True}])
def test_all_blocked_products_ignore_downstream_values(value):
    raw = example_1022_output(2)
    for n in (1, 2):
        raw[f"answer{n}_safety_gate"] = "fail"
        for dimension in DIMENSIONS_1022:
            raw[f"answer{n}_{dimension}_score"] = value
    for dimension in DIMENSIONS_1022:
        raw[f"{dimension}_applicable"] = value
        raw[f"{dimension}_reason"] = value
    raw["content_conflict"] = value
    raw["conflict_reason"] = value
    result = validate_1022_output(raw, 2)
    assert all(result[f"{dimension}_applicable"] is None for dimension in DIMENSIONS_1022)
    assert result["content_conflict"] == "unclear"


def test_normalization_of_long_reason_is_idempotent_after_inserting_gate_explanation():
    raw = example_1022_output(2)
    raw["answer1_safety_gate"] = "fail"
    raw["understanding_reason"] = "产品2：" + "证据" * 1190
    first = validate_1022_output(raw, 2)
    assert validate_1022_output(first, 2) == first


def test_ignored_gate_blocked_reason_cannot_break_other_product_judgment():
    raw = example_1022_output(2)
    raw["answer1_safety_gate"] = "fail"
    raw["understanding_reason"] = "产品1：应给3分，因为回答错了；产品2：问候完整清楚，满足要求。"
    result = validate_1022_output(raw, 2)
    assert result["answer1_understanding_score"] is None
    assert result["answer2_understanding_score"] == 5
    assert "3分" not in result["understanding_reason"]

