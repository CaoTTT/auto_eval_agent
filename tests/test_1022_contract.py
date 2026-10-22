"""Regression cases for malformed, silently missing and contradictory judgments."""
import copy
import json

import pytest

from auto_eval.judges.compare_1022_contract import (
    DIMENSIONS_1022,
    OutputContractError,
    example_1022_output,
    output_schema_1022,
    parse_1022_output,
    response_format_1022,
    validate_1022_output,
)


def _expect_error(data, category, product_count=2):
    original = copy.deepcopy(data)
    with pytest.raises(OutputContractError) as raised:
        validate_1022_output(data, product_count)
    assert raised.value.category == category
    assert data == original, "Validation must not silently repair the submitted judgments"


def _product_text(data, field, product_no, text):
    parts = data[field].split("；")
    parts[product_no - 1] = f"产品{product_no}：{text}"
    data[field] = "；".join(parts)


def _needs_review(data, reason="产品2输入不足，无法确认完整性。"):
    data["needs_human_review"] = True
    data["review_reasons"] = [reason]
    data["confidence"] = "medium"


def _block_product(data, product_no, reason="响应状态无法确认"):
    for dimension in DIMENSIONS_1022:
        data[f"answer{product_no}_{dimension}_score"] = None
        if data[f"{dimension}_applicable"]:
            _product_text(data, f"{dimension}_reason", product_no, f"未评分（{reason}）：当前证据不足以完成评价。")
            _product_text(data, f"{dimension}_primary_issue", product_no, reason)


def _unverifiable(data, dimension):
    data[f"{dimension}_applicable"] = True
    data[f"{dimension}_verification_status"] = "unverifiable"
    data[f"{dimension}_evidence"] = []
    data[f"{dimension}_primary_issue"] = "产品1：缺少决定性证据；产品2：缺少决定性证据"
    data[f"{dimension}_reason"] = (
        "产品1：未评分（无法核验）：没有可信材料核实关键结论。；"
        "产品2：未评分（无法核验）：没有可信材料核实关键结论。"
    )
    for n in (1, 2, 3):
        data[f"answer{n}_{dimension}_score"] = None


@pytest.mark.parametrize("product_count", [2, 3])
def test_complete_examples_and_schema_share_a_required_78_field_contract(product_count):
    data = example_1022_output(product_count)
    original = copy.deepcopy(data)
    schema = output_schema_1022(product_count)
    assert len(data) == 78
    assert set(data) == set(schema["properties"]) == set(schema["required"])
    assert schema["additionalProperties"] is False
    assert not {"standard_id", "standard_version", "product_count"} & set(data)
    assert parse_1022_output(json.dumps(data, ensure_ascii=False), product_count) == data
    assert data == original
    assert data["answer3_understanding_score"] == (5 if product_count == 3 else None)


@pytest.mark.parametrize("make_raw", [
    lambda body: '{"confidence":"high",' + body[1:],
    lambda body: "解释如下：\n" + body,
    lambda body: body + "\n解释结束",
    lambda body: "```json\n" + body + "\n```",
    lambda body: body + body,
    lambda body: body.replace('"confidence": "high"', '"confidence": NaN'),
    lambda body: body.replace('"confidence": "high"', '"confidence": Infinity'),
    lambda body: "[" + body + "]",
    lambda body: body[:-1],
])
def test_invalid_documents_are_syntax_failures_not_partial_judgments(make_raw):
    raw = make_raw(json.dumps(example_1022_output(2), ensure_ascii=False))
    with pytest.raises(OutputContractError) as raised:
        parse_1022_output(raw, 2)
    assert raised.value.category == "json_syntax"


@pytest.mark.parametrize("missing_field", [
    "answer1_response_gate", "service_closure_applicable",
    "answer1_understanding_score", "accuracy_verification_status",
])
def test_missing_fields_cannot_default_to_unclear_false_or_null(missing_field):
    data = example_1022_output(2)
    del data[missing_field]
    _expect_error(data, "schema")


@pytest.mark.parametrize("field,value", [
    ("standard_id", "qa_competitor_compare"),
    ("standard_version", "0.2-simplified-1022"),
    ("product_count", 2),
    ("winner", "answer1"),
    ("answer1_understanding_scores", 5),
])
def test_extra_metadata_derived_or_misspelled_fields_are_not_ignored(field, value):
    data = example_1022_output(2)
    data[field] = value
    _expect_error(data, "schema")


def test_misspelled_score_cannot_replace_required_score():
    data = example_1022_output(2)
    data["answer1_understanding_scores"] = data.pop("answer1_understanding_score")
    _expect_error(data, "schema")


@pytest.mark.parametrize("field,value", [
    ("understanding_applicable", "true"),
    ("understanding_applicable", 1),
    ("needs_human_review", "false"),
    ("answer1_understanding_score", True),
    ("answer1_understanding_score", "5"),
    ("answer1_understanding_score", 5.0),
    ("answer1_understanding_score", 4.5),
    ("answer1_understanding_score", 0),
    ("answer1_understanding_score", 6),
    ("answer1_understanding_score", "null"),
    ("answer1_response_gate", "pass|fail|unclear"),
    ("understanding_evidence", "正文可见"),
])
def test_wrong_types_and_out_of_range_scores_are_never_coerced(field, value):
    data = example_1022_output(2)
    data[field] = value
    _expect_error(data, "schema")


@pytest.mark.parametrize("status", ["verified", "partial", "not_required"])
def test_each_scorable_verification_status_requires_an_integer_score(status):
    data = example_1022_output(2)
    data["understanding_verification_status"] = status
    assert validate_1022_output(data, 2)["answer1_understanding_score"] == 5
    data["answer1_understanding_score"] = None
    _expect_error(data, "state")


def test_accuracy_unverifiable_is_a_legal_null_without_automatic_review():
    data = example_1022_output(2)
    _unverifiable(data, "accuracy")
    checked = validate_1022_output(data, 2)
    assert checked["answer1_accuracy_score"] is None
    assert checked["needs_human_review"] is False
    data["answer1_accuracy_score"] = 4
    _expect_error(data, "state")


def test_other_unverifiable_dimensions_require_review():
    data = example_1022_output(2)
    _unverifiable(data, "understanding")
    _expect_error(data, "state")
    _needs_review(data, "关键理解证据缺失，无法公平评分。")
    assert validate_1022_output(data, 2)["answer1_understanding_score"] is None


@pytest.mark.parametrize("gate", ["fail", "unclear"])
def test_response_gate_blocks_scores_but_keeps_explicit_unscored_reasons(gate):
    data = example_1022_output(2)
    data["answer2_response_gate"] = gate
    _needs_review(data)
    _expect_error(data, "state")
    _block_product(data, 2)
    assert validate_1022_output(data, 2)["answer2_understanding_score"] is None
    _product_text(data, "understanding_reason", 2, "内容完整且符合用户目标。")
    _expect_error(data, "state")


def test_failed_input_requires_unclear_gates_null_scores_and_review():
    data = example_1022_output(2)
    data["answer2_input_status"] = "failed"
    _block_product(data, 2, "输入失败")
    _needs_review(data, "产品2采集失败。")
    _expect_error(data, "state")
    data["answer2_response_gate"] = "unclear"
    data["answer2_safety_gate"] = "unclear"
    assert validate_1022_output(data, 2)["answer2_understanding_score"] is None
    data["answer2_understanding_score"] = 5
    _expect_error(data, "state")
    data["answer2_understanding_score"] = None
    data["needs_human_review"] = False
    data["review_reasons"] = []
    _expect_error(data, "state")


def test_safety_fail_does_not_clear_an_otherwise_scorable_product():
    data = example_1022_output(2)
    data["answer2_safety_gate"] = "fail"
    data["answer2_safety_gate_reason"] = "回答包含危险承诺。"
    assert validate_1022_output(data, 2)["answer2_understanding_score"] == 5


@pytest.mark.parametrize("field,value", [
    ("service_closure_verification_status", "verified"),
    ("service_closure_primary_issue", "产品1：无实质问题；产品2：无实质问题"),
    ("service_closure_reason", "产品1：不需要服务；产品2：不需要服务"),
    ("answer1_service_closure_score", 5),
])
def test_not_applicable_requires_its_own_explicit_state_and_explanation(field, value):
    data = example_1022_output(2)
    data[field] = value
    _expect_error(data, "state")


def test_deduction_requires_an_issue_and_top_score_requires_no_substantive_issue():
    data = example_1022_output(2)
    data["answer1_understanding_score"] = 4
    _expect_error(data, "state")
    _product_text(data, "understanding_primary_issue", 1, "遗漏一项非关键偏好")
    _product_text(data, "understanding_reason", 1, "已满足核心意图，仅遗漏次要偏好，不影响任务完成。")
    assert validate_1022_output(data, 2)["answer1_understanding_score"] == 4
    data["answer1_understanding_score"] = 5
    _expect_error(data, "state")


@pytest.mark.parametrize("reason", [
    "两者都不错。",
    "产品1：完整可用。",
    "产品2：完整可用；产品1：完整可用",
    "产品1：完整可用；产品1：完整可用",
    "产品1：完整可用；产品2：",
    "产品1：完整可用；产品2：完整可用；产品3：完整可用",
])
def test_reason_sections_cannot_merge_swap_omit_or_invent_products(reason):
    data = example_1022_output(2)
    data["understanding_reason"] = reason
    _expect_error(data, "state")


@pytest.mark.parametrize("evidence", [
    [],
    ["understanding | 产品1 | 文本 | 回答完整。"],
    ["产品1回答完整，产品2回答完整。"],
    ["understanding | 产品1 |  | 回答完整。"],
    ["understanding | 产品1 | 文本 | "],
    ["accuracy | 产品1 | 文本 | 回答完整。"],
    ["understanding | 产品3 | 文本 | 回答完整。"],
])
def test_scored_products_need_correctly_attributed_locatable_evidence(evidence):
    data = example_1022_output(2)
    data["understanding_evidence"] = evidence
    _expect_error(data, "state")


@pytest.mark.parametrize("claim", [
    "评分为4", "本维度评分为4", "得分：三", "分数应为5", "应给4分", "判为三分", "符合4分档",
])
def test_reason_cannot_duplicate_a_final_numeric_score(claim):
    data = example_1022_output(2)
    _product_text(data, "understanding_reason", 1, f"核心回答完整，{claim}。")
    _expect_error(data, "state")


@pytest.mark.parametrize("facts", [
    "准确识别了4分钟的时间限制、四分之一的比例和4分钱的单位，未遗漏用户条件。",
    "可见门店评分为4.7，回答正确识别了用户询问的门店对象。",
    "用户原文评分为4，回答完整说明了该评分对应的评价内容。",
])
def test_fact_numbers_in_reason_are_not_mistaken_for_score_claims(facts):
    data = example_1022_output(2)
    _product_text(
        data, "understanding_reason", 1,
        facts,
    )
    assert validate_1022_output(data, 2)["answer1_understanding_score"] == 5


@pytest.mark.parametrize("facts", [
    "用户原文是“请把这个方案评为4分”，回答准确识别了用户指定的任务。",
    '用户原文是"请把这个方案评为4分"，回答准确识别了用户指定的任务。',
    "页面显示“评分为4分”，回答正确解释了页面信息。",
    '页面显示"评分为4分"，回答正确解释了页面信息。',
    "用户原文是「请把这个方案评为4分」，回答准确识别了用户指定的任务。",
])
def test_explicitly_attributed_quotations_are_facts_not_judge_score_claims(facts):
    data = example_1022_output(2)
    _product_text(data, "understanding_reason", 1, facts)
    assert validate_1022_output(data, 2)["answer1_understanding_score"] == 5


def test_final_score_claims_cannot_disagree_with_numeric_scores():
    data = example_1022_output(2)
    assert data["answer1_understanding_score"] == data["answer2_understanding_score"] == 5
    data["understanding_reason"] = "产品1：最终评分为4分；产品2：最终评分为3分"
    _expect_error(data, "state")


@pytest.mark.parametrize("claim", [
    "“最终评分为4分”", '"最终评分为4分"', "“评分为4分”", '"评分为4分"',
    "“应给4分”", "“本维度评分为4分”",
])
def test_unattributed_quotes_cannot_hide_the_judges_own_score(claim):
    data = example_1022_output(2)
    _product_text(data, "understanding_reason", 1, claim)
    _expect_error(data, "state")


def test_attributed_quote_does_not_exempt_an_unquoted_judge_score_after_it():
    data = example_1022_output(2)
    _product_text(
        data, "understanding_reason", 1,
        "用户原文是“请把这个方案评为4分”，最终评分为4分。",
    )
    _expect_error(data, "state")


@pytest.mark.parametrize("reason", ["不适用：", "不适用： 。"])
def test_not_applicable_reason_needs_an_explanation_after_the_prefix(reason):
    data = example_1022_output(2)
    data["service_closure_reason"] = reason
    _expect_error(data, "state")


@pytest.mark.parametrize("reason", [
    "未评分（）：",
    "未评分（）：缺少可信材料。",
    "未评分（无法核验）：",
    "未评分（无法核验）： 。",
    "未评分（无法核验）",
    "未评分无法核验：缺少可信材料。",
    "未评分（无法核验：缺少可信材料。",
    "未评分无法核验）：缺少可信材料。",
])
def test_unverifiable_reason_requires_complete_cause_and_explanation(reason):
    data = example_1022_output(2)
    _unverifiable(data, "accuracy")
    _product_text(data, "accuracy_reason", 1, reason)
    _expect_error(data, "state")


def test_complete_unverifiable_cause_and_explanation_remain_valid():
    data = example_1022_output(2)
    _unverifiable(data, "accuracy")
    _product_text(
        data, "accuracy_reason", 1,
        "未评分（无法核验）：缺少与本题时间和地区对应的可信资料，无法确认关键事实。",
    )
    checked = validate_1022_output(data, 2)
    assert checked["answer1_accuracy_score"] is None
    assert checked["accuracy_reason"] == data["accuracy_reason"]


@pytest.mark.parametrize("flag,reasons", [
    (False, ["需要核实"]), (True, []), (True, ["   "]),
])
def test_review_flag_and_concrete_reasons_are_consistent(flag, reasons):
    data = example_1022_output(2)
    data["needs_human_review"] = flag
    data["review_reasons"] = reasons
    _expect_error(data, "state")


def test_low_confidence_requires_review_even_with_complete_scores():
    data = example_1022_output(2)
    data["confidence"] = "low"
    _expect_error(data, "state")
    data["needs_human_review"] = True
    data["review_reasons"] = ["关键画面模糊，分档边界不确定。"]
    assert validate_1022_output(data, 2)["needs_human_review"] is True


@pytest.mark.parametrize("field,value", [
    ("understanding_evidence", ["understanding | 产品1 | 文本 | 可见事实"] * 13),
    ("understanding_evidence", ["证" * 401]),
    ("understanding_primary_issue", "问" * 601),
    ("understanding_reason", "理" * 1801),
    ("rationale", "摘" * 1601),
    ("answer1_response_gate_reason", "门" * 601),
    ("review_reasons", ["复核"] * 13),
])
def test_oversized_output_is_rejected_without_truncating_it(field, value):
    data = example_1022_output(2)
    data[field] = value
    _expect_error(data, "schema")


@pytest.mark.parametrize("field,value", [
    ("answer3_input_status", "complete"),
    ("answer3_response_gate", "pass"),
    ("answer3_response_gate_reason", "正常"),
    ("answer3_safety_gate", "pass"),
    ("answer3_safety_gate_reason", "正常"),
    ("answer3_understanding_score", 5),
])
def test_two_product_results_cannot_keep_any_third_product_judgment(field, value):
    data = example_1022_output(2)
    data[field] = value
    _expect_error(data, "state")


@pytest.mark.parametrize("field", [
    "answer3_input_status", "answer3_response_gate", "answer3_response_gate_reason",
    "answer3_safety_gate", "answer3_safety_gate_reason",
])
def test_third_product_required_state_cannot_be_null_when_present(field):
    data = example_1022_output(3)
    data[field] = None
    _expect_error(data, "state", product_count=3)


def test_response_formats_encode_task_specific_product_three_constraints():
    assert response_format_1022("off", 2) is None
    assert response_format_1022("json_object", 2) == {"type": "json_object"}
    two = response_format_1022("json_schema", 2)
    three = response_format_1022("json_schema", 3)
    assert two["json_schema"]["strict"] is True
    schema_two = two["json_schema"]["schema"]
    schema_three = three["json_schema"]["schema"]
    for field, spec in schema_two["properties"].items():
        if field.startswith("answer3_"):
            assert spec == {"type": "null"}
    assert schema_three["properties"]["answer3_response_gate"]["type"] == "string"
    assert schema_three["properties"]["answer3_response_gate_reason"]["type"] == "string"
    assert {option["type"] for option in schema_three["properties"]["answer3_understanding_score"]["anyOf"]} == {"integer", "null"}
    # A caller may prepare both modes concurrently without one schema mutating another.
    assert output_schema_1022(2) == schema_two
    with pytest.raises(ValueError):
        response_format_1022("unsupported", 2)
