"""Conservative format compatibility; synthetic cases do not measure model quality."""
from copy import deepcopy

import pytest

from auto_eval.judges.compare_1022_contract import (
    DIMENSIONS_1022, OutputContractError, example_1022_output,
    product_reason_1022, validate_1022_output,
)


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("heading", [
    "产品{n}：", "产品 {wide} : ", "- 产品{wide} ： ",
    "* 产品{n}：", "{n}. 产品{wide}：", "（{n}）【产品 {wide}】：",
    "【产品{n}】：", "(产品{wide}) : ", "**产品{wide}**：", "**产品{n}：**",
])
def test_explicit_equivalent_labels_normalize_without_changing_judgments(count, heading):
    raw = example_1022_output(count)
    parts = {n: f"第1帧显示回答覆盖目标{n}，可以直接理解。" for n in range(1, count + 1)}
    raw["understanding_reason"] = "\n".join(
        heading.format(n=n, wide=str(n).translate(str.maketrans("123", "１２３"))) + parts[n]
        for n in parts
    )
    original = deepcopy(raw)
    result = validate_1022_output(raw, count)
    assert raw == original
    assert result["understanding_reason"] == "；".join(f"产品{n}：{parts[n]}" for n in parts)
    assert {key: value for key, value in result.items() if key != "understanding_reason"} == {
        key: value for key, value in original.items() if key != "understanding_reason"
    }
    for n in parts:
        assert product_reason_1022(original["understanding_reason"], n) == parts[n]
        assert product_reason_1022(result["understanding_reason"], n) == parts[n]
    assert validate_1022_output(result, count) == result


@pytest.mark.parametrize("prefix", [
    "本Query要求可核验依据，因此有理有据适用。\n",
    "Query级适用性：本题需要外部来源。；",
    "当前任务需要来源，维度适用。; ",
    "本维度适用：需要说明来源与正文的关系。\n\n",
    "总体不错；",
    "可选模式：各产品可按可见展示评价。\n",
    "本Query需要依据，产品1补充说明较少；",
    "本维度适用，P2来源需要进一步识别；",
    "产品2的主动展示触发本维度，产品1仍可按规则留空。\n",
])
@pytest.mark.parametrize("blocked", [False, True])
def test_query_level_preface_is_preserved_and_not_assigned_to_a_product(prefix, blocked):
    raw = example_1022_output(2)
    raw["evidence_quality_applicable"] = True
    for n in (1, 2):
        raw[f"answer{n}_evidence_quality_score"] = 4
    raw["evidence_quality_reason"] = prefix + "- 产品１：正文引用与结论就近对应。\n- 产品２：来源身份可见。"
    if blocked:
        raw["answer1_safety_gate"] = "fail"
    result = validate_1022_output(raw, 2)
    assert result["evidence_quality_reason"].startswith(prefix)
    assert product_reason_1022(result["evidence_quality_reason"], 2) == "来源身份可见。"
    assert "本Query" not in product_reason_1022(result["evidence_quality_reason"], 1)
    assert result["answer2_evidence_quality_score"] == 4
    assert result["answer1_evidence_quality_score"] == (None if blocked else 4)
    assert validate_1022_output(result, 2) == result


@pytest.mark.parametrize("left,right", [("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ('"', '"'), ("`", "`")])
def test_quoted_product_labels_do_not_create_sections_or_override_real_attribution(left, right):
    raw = example_1022_output(2)
    first = f"回答原文：{left}配置如下；产品2：未评分（证据不足）：不能识别。{right}，本产品说明完整。"
    second = "未评分（证据不足）：本产品截图无法辨认核心文字。"
    raw["understanding_reason"] = f"产品1：{first}\n产品２：{second}"
    raw["answer2_understanding_score"] = None
    result = validate_1022_output(raw, 2)
    assert product_reason_1022(result["understanding_reason"], 1) == first
    assert product_reason_1022(result["understanding_reason"], 2) == second
    assert result["answer1_understanding_score"] == 5
    assert result["answer2_understanding_score"] is None
    assert validate_1022_output(result, 2) == result


def test_quote_cannot_supply_a_missing_product_reason():
    raw = example_1022_output(2)
    raw["understanding_reason"] = "产品1：回答原文：“第一项完整；产品2：第二项完整。”，内容清晰。"
    with pytest.raises(OutputContractError, match="缺少未被Gate阻断产品"):
        validate_1022_output(raw, 2)


def test_quote_mask_does_not_exempt_the_judges_own_numeric_grading_claim():
    raw = example_1022_output(2)
    raw["understanding_reason"] = "产品1：“应给3分”，因为漏掉了目标。；产品2：回答完整。"
    with pytest.raises(OutputContractError, match="数值评分"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("reason", [
    "本维度适用，评分为4分；产品1：完整；产品2：清楚",
    "产品１：完整\n- 产品１：另一个结论\n产品２：清楚",
    "产品２：清楚\n产品１：完整",
    "产品1：完整\n产品4：清楚",
    "产品1：完整\n产品2：；",
    "产品1：完整\n产品1/2：二者都完整",
])
def test_ambiguous_attribution_or_grading_claim_remains_rejected(reason):
    raw = example_1022_output(2)
    raw["understanding_reason"] = reason
    with pytest.raises(OutputContractError):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("separator", ["。", "！", "？", "!", "?"])
def test_sentence_boundaries_between_explicit_labels_preserve_sentence_punctuation(separator):
    raw = example_1022_output(2)
    raw["understanding_reason"] = f"产品1：核心回答完整{separator}产品2：次要说明清楚。"
    result = validate_1022_output(raw, 2)
    assert product_reason_1022(result["understanding_reason"], 1) == f"核心回答完整{separator}"
    assert product_reason_1022(result["understanding_reason"], 2) == "次要说明清楚。"
    assert validate_1022_output(result, 2) == result


@pytest.mark.parametrize("reason,category", [
    ("第1块正文未见来源展示，属于可选未出现，不因此扣分。", "可选未出现"),
    ("第1块没有服务承接；服务属可选，未提供时按规则型NA处理。", "可选未出现"),
    ("第1块未见引用标记；来源为可选，未出现时无需评分。", "可选未出现"),
    ("来源为可选，截图确认未出现正文引用。", "可选未出现"),
    ("第1块服务属禁止，未出现服务入口，遵守任务边界。", "禁止未出现"),
    ("第1块正文未见来源，按规则型NA处理。", "规则型NA"),
    ("第1块无法确认按钮是否为引用标记，来源身份不可辨；证据不足，无法公平判断。", "证据不足"),
    ("第1块服务可选未出现。未评分（可选未出现）：未影响核心回答。", "可选未出现"),
])
def test_explicit_null_and_existing_specific_na_explanation_gain_only_a_display_marker(reason, category):
    raw = example_1022_output(2)
    raw["understanding_reason"] = f"产品1：{reason}；产品2：回答清楚。"
    raw["answer1_understanding_score"] = None
    result = validate_1022_output(raw, 2)
    assert product_reason_1022(result["understanding_reason"], 1) == f"未评分（{category}）：{reason}"
    assert result["answer1_understanding_score"] is None
    assert result["answer2_understanding_score"] == 5
    assert validate_1022_output(result, 2) == result
    del raw["answer1_understanding_score"]
    with pytest.raises(OutputContractError, match="缺失"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("reason", [
    "证据不足", "证据不足，无法公平判断。", "可选未出现", "规则型NA",
    "可选来源已出现，但未展示机构名称。", "只显示引用图标，无法确认类型。",
])
def test_vague_or_ambiguous_null_reason_is_not_completed(reason):
    raw = example_1022_output(2)
    raw["understanding_reason"] = f"产品1：{reason}；产品2：回答清楚。"
    raw["answer1_understanding_score"] = None
    with pytest.raises(OutputContractError, match="空分须用"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("reason", [
    "截图不存在证据不足，核心文字清晰，没有任何关键内容缺失。",
    "第1块明确显示来源，不能认定证据不足；不存在截图缺失或遮挡，事实均可确认。",
    "第1块服务已提供，不能按可选未出现处理，当前维度应评分。",
    "来源不是可选，未出现本应扣分。",
    "并非规则型NA，正文引用并未缺失，不能认为未出现。",
    "证据充足，所有关键信息均可确认，不应认定证据不足；没有截图遮挡。",
    "可选来源已出现，但其中少数未展示；按规则型NA处理。",
    "禁止服务并非未出现，截图已显示服务入口。",
])
def test_negated_or_contradictory_explanation_is_not_wrapped_as_a_justified_null(reason):
    from auto_eval.judges.compare_1022_contract import _normalize_explicit_unscored_reason

    assert _normalize_explicit_unscored_reason(reason) == reason
    raw = example_1022_output(2)
    raw["understanding_reason"] = f"产品1：{reason}；产品2：回答清楚。"
    raw["answer1_understanding_score"] = None
    with pytest.raises(OutputContractError, match="空分须用"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("text", ["预算1500～2500分档清楚。", "年收入20000分档清楚。", "数量四十五分档清楚。"])
def test_multidigit_numeric_brackets_are_not_mistaken_for_judge_grades(text):
    raw = example_1022_output(2)
    raw["scenario_fulfillment_applicable"] = True
    raw["scenario_fulfillment_reason"] = f"产品1：第1块{text}；产品2：说明清楚。"
    for n in (1, 2):
        raw[f"answer{n}_scenario_fulfillment_score"] = 4
    result = validate_1022_output(raw, 2)
    assert text in result["scenario_fulfillment_reason"]


@pytest.mark.parametrize("claim", ["评4分", "5分档", "预算2500分档，评4分", "4.5分档", "预算2500回答原文：“条件已满足”5分档"])
def test_real_grading_claims_still_fail_next_to_numeric_brackets(claim):
    raw = example_1022_output(2)
    raw["understanding_reason"] = f"产品1：{claim}，因为缺少条件。；产品2：说明清楚。"
    with pytest.raises(OutputContractError, match="数值评分"):
        validate_1022_output(raw, 2)


@pytest.mark.parametrize("missing", ["answer2_understanding_score", "answer2_response_gate", "answer2_safety_gate_reason"])
def test_format_compatibility_does_not_supply_missing_scores_or_gates(missing):
    raw = example_1022_output(2)
    raw["understanding_reason"] = "本Query需要理解问候需求。\n产品１：完整回应。\n产品２：清楚回应。"
    del raw[missing]
    with pytest.raises(OutputContractError):
        validate_1022_output(raw, 2)


def test_gate_shortcut_preserves_query_preface_and_checks_only_active_product_judgments():
    raw = example_1022_output(2)
    raw["answer1_safety_gate"] = "fail"
    raw["understanding_reason"] = (
        "本Query需要理解问候需求。\n产品１：应给3分，理由被Gate阻断。\n产品２：清楚回应问候。"
    )
    result = validate_1022_output(raw, 2)
    assert result["understanding_reason"].startswith("本Query需要理解问候需求。\n")
    assert "3分" not in result["understanding_reason"]
    assert product_reason_1022(result["understanding_reason"], 1).startswith("未评分（安全Gate未通过）")
    assert product_reason_1022(result["understanding_reason"], 2) == "清楚回应问候。"
    assert all(result[f"answer1_{dimension}_score"] is None for dimension in DIMENSIONS_1022)
    assert validate_1022_output(result, 2) == result


def test_duplicate_sections_are_not_silently_resolved_by_product_extraction():
    assert product_reason_1022("产品1：第一种；产品1：另一种；产品2：正常", 1) == ""


def test_normalized_reason_keeps_export_and_statistics_product_na_attribution():
    from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol
    from auto_eval.web import compare_statistics as stats, history

    raw = example_1022_output(2)
    raw["understanding_reason"] = (
        "本Query需要理解用户指代。\n（1）产品１：问候完整。\n（2）产品２：未评分（证据不足）：截图核心文字不可辨。"
    )
    raw["answer2_understanding_score"] = None
    result = validate_1022_output(raw, 2)
    protocol = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID)
    result.update(product_count=2, standard_id=protocol.standard_id, standard_version=protocol.standard_version,
                  evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
                  output_contract=protocol.output_contract)
    case = stats.Case(0, {}, result, "done", 2, protocol.standard_version, protocol.bundle_revision)
    assert stats.score_state(case, 1, "understanding", protocol) == ("有效评分", 5)
    assert stats.score_state(case, 2, "understanding", protocol) == ("证据不足", None)
    row = history._visual_compare_export_rows([result])[0]
    assert row["理解需求理由"] == result["understanding_reason"]
    assert row["产品2理解需求分"] == "N/A"
