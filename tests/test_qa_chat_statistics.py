"""Known-answer checks for independent chat scores and conversation weighting."""
from copy import deepcopy
from html import escape
from xml.etree import ElementTree as ET

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import compare_statistics as stats
from auto_eval.web.compare_statistics_xlsx import statistics_sheet_xml


def dataset(scores, *, count=None):
    protocol = resolve_compare_protocol("qa_chat_compare@1.0")
    count = count or len(scores[0])
    snapshot = {"mode": "compare", "evaluation_profile": protocol.id,
                "protocol_manifest": protocol.public_metadata(), "items": []}
    rows = []
    for index, values in enumerate(scores):
        snapshot["items"].append({"id": f"c{index}", "query": f"问候{index}", "product_count": count})
        row = {"评估状态": "已完成", "standard_id": protocol.standard_id,
               "standard_version": protocol.standard_version, "evaluation_profile": protocol.id,
               "bundle_revision": protocol.bundle_revision, "product_count": count}
        for p, value in enumerate(values, 1):
            row.update({f"answer{p}_input_status": "complete", f"answer{p}_response_gate": "pass",
                        f"answer{p}_safety_gate": "pass"})
            for dimension in protocol.dimensions:
                row.update({f"answer{p}_{dimension}_applicable": True,
                            f"answer{p}_{dimension}_verification_status": "verified" if value is not None else "unverifiable",
                            f"answer{p}_{dimension}_score_status": "scored" if value is not None else "unverifiable",
                            f"answer{p}_{dimension}_score": value})
        rows.append(row)
    return snapshot, rows, protocol


def change(row, product=1, dimension="personalization", **values):
    row.update({f"answer{product}_{dimension}_{key}": value for key, value in values.items()})


def table(tables, prefix):
    return next(t for t in tables if t.title.startswith(prefix))


def record(tables, prefix, **match):
    selected = table(tables, prefix)
    for values in selected.rows:
        row = dict(zip(selected.headers, values))
        if all(row.get(key) == value for key, value in match.items()):
            return row
    raise AssertionError(f"Missing {prefix}: {match}")


def report(snapshot, rows):
    return stats.build_compare_statistics(snapshot, rows)


@pytest.mark.parametrize("count", (2, 3))
def test_chat_dynamic_dimensions_all_directions_and_no_legacy_columns(count):
    snapshot, rows, protocol = dataset([[5, 4, 3][:count], [1, 2, 5][:count]])
    tables = report(snapshot, rows)
    assert {row[0] for row in table(tables, "B2．").rows} == set(protocol.dimension_names.values())
    assert {row[0] for row in table(tables, "C1．").rows} == set(protocol.dimension_names.values())
    assert len(table(tables, "C1．").rows) == 6 * count * (count - 1)
    assert {row[2] for row in table(tables, "B3．").rows} == {1, 2, 3, 4, 5}
    assert any(t.title.startswith("D．") for t in tables) == (count == 3)
    assert not any("产品3" in str(row) for t in tables for row in t.rows) if count == 2 else True
    assert all(len(row) == len(t.headers) for t in tables for row in t.rows)
    assert all(not t.formats or len(t.formats) == len(t.headers) for t in tables)


def test_chat_product_authority_keeps_valid_score_despite_shared_unverifiable():
    snapshot, rows, protocol = dataset([[5, 4]])
    rows[0].update(personalization_applicable=False, personalization_verification_status="unverifiable",
                   answer1_response_gate="unclear", answer1_input_status="partial", needs_human_review=True)
    change(rows[0], 1, verification_status="partial")
    change(rows[0], 2, verification_status="unverifiable", score_status="unverifiable", score=None)
    cases = stats._cases(snapshot, rows)
    assert stats.score_state(cases[0], 1, "personalization", protocol) == ("有效评分", 5.0)
    assert stats.score_state(cases[0], 2, "personalization", protocol) == ("无法核验", None)
    tables = report(snapshot, rows)
    p1 = record(tables, "B2．", 维度="个性化适配", 产品="产品1")
    assert p1["有效题数"] == p1["受限证据题数"] == 1 and p1["均分"] == 5
    assert record(tables, "B1．", 对象="个性化适配", 指标="共享摘要不一致")["数量"] == 1
    assert record(tables, "B2．", 维度="个性化适配", 产品="产品2")["有效题数"] == 0
    assert record(tables, "C1．", 维度="个性化适配", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == 0


@pytest.mark.parametrize("value", [0, 6, True, "4", 4.0, 2.5, float("nan"), float("inf")])
def test_chat_score_state_strict_integers(value):
    snapshot, rows, protocol = dataset([[5, 4]])
    change(rows[0], score=value)
    assert stats.score_state(stats._cases(snapshot, rows)[0], 1, "personalization", protocol) == ("分数格式或范围异常", None)


def test_chat_priority_input_then_gate_then_applicability_and_missing_status():
    snapshot, rows, protocol = dataset([[5, 4]])
    case = stats._cases(snapshot, rows)[0]
    change(rows[0], applicable=False, verification_status="not_required", score_status="not_applicable", score=None)
    rows[0]["answer1_safety_gate"] = "fail"
    assert stats.score_state(case, 1, "personalization", protocol) == ("安全Gate阻断", None)
    rows[0]["answer1_input_status"] = "failed"
    assert stats.score_state(case, 1, "personalization", protocol) == ("输入失败", None)
    rows[0].update(answer1_input_status="complete", answer1_safety_gate="pass")
    assert stats.score_state(case, 1, "personalization", protocol) == ("维度不适用", None)
    del rows[0]["answer1_personalization_score_status"]
    assert stats.score_state(case, 1, "personalization", protocol) == ("评分状态缺失", None)


def test_chat_coverage_uses_independent_known_applicability_denominators():
    snapshot, rows, _ = dataset([[5, 4], [4, 3], [3, 2], [2, 1]])
    rows[1]["answer1_response_gate"] = "fail"
    change(rows[1], score_status="gate_blocked", score=None)
    change(rows[2], applicable=None, verification_status="unverifiable", score_status="unverifiable", score=None)
    change(rows[3], applicable=False, verification_status="not_required", score_status="not_applicable", score=None)
    tables = report(snapshot, rows)
    subject = "个性化适配 / 产品1"
    coverage = record(tables, "B1．", 对象=subject, 指标="有效评分覆盖率")
    assert (coverage["数量"], coverage["分母"], coverage["占比"]) == (1, 2, .5)
    assert record(tables, "B1．", 对象=subject, 指标="维度适用率")["占比"] == pytest.approx(2 / 3)
    assert record(tables, "B1．", 对象=subject, 指标="适用性未知数")["数量"] == 1
    assert record(tables, "B2．", 维度="个性化适配", 产品="产品2")["有效题数"] == 4
    pair = record(tables, "C1．", 维度="个性化适配", **{"方向 A/B": "产品1 / 产品2"})
    assert (pair["适用题数"], pair["配对有效题数"], pair["适用未知题数"]) == (2, 1, 1)


def test_chat_third_input_failure_preserves_ab_and_separates_three_way_common_set():
    snapshot, rows, _ = dataset([[5, 4, 3], [1, 2, 5]])
    rows[1]["answer3_input_status"] = "failed"
    for dimension in resolve_compare_protocol("qa_chat_compare@1.0").dimensions:
        change(rows[1], 3, dimension, score_status="input_failed", score=None, verification_status=None)
    tables = report(snapshot, rows)
    assert record(tables, "B2．", 维度="理解需求", 产品="产品1")["有效题数"] == 2
    assert record(tables, "B2．", 维度="理解需求", 产品="产品3")["有效题数"] == 1
    assert record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == 2
    common = record(tables, "C1．双向分位值与配对分差（三方", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert common["配对有效题数"] == 1
    assert record(tables, "D．", 维度="理解需求", 产品="产品1")["三方有效题数"] == 1


def test_design_hand_calculated_asymmetric_three_product_example():
    snapshot, rows, _ = dataset([[5, 3, None], [1, 3, 4], [4, None, 4]])
    rows[0]["answer1_response_gate"] = "unclear"
    tables = report(snapshot, rows)
    for product, expected in (("产品1", 3), ("产品2", 2), ("产品3", 2)):
        assert record(tables, "B2．", 维度="理解需求", 产品=product)["有效题数"] == expected
    for direction, n, ratio in (("产品1 / 产品2", 2, 1), ("产品1 / 产品3", 2, .625), ("产品2 / 产品3", 1, .75)):
        pair = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": direction})
        assert pair["配对有效题数"] == n
        assert pair["分位值 A/B"] == ratio
    assert record(tables, "C2．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["GSB数量"] == "1/0/1"
    assert record(tables, "D．", 维度="理解需求", 产品="产品3")["独占第一数"] == 1
    assert record(tables, "D．", 维度="理解需求", 产品="产品1")["独占末位数"] == 1


def test_chat_all_five_bins_thresholds_and_blocked_applicability_denominator():
    snapshot, rows, _ = dataset([[value, 3] for value in range(1, 6)])
    performance = record(report(snapshot, rows), "B2．", 维度="理解需求", 产品="产品1")
    assert performance["均分"] == performance["中位数"] == 3
    assert performance["达标率"] == performance["低质率"] == .4
    snapshot, rows, _ = dataset([[5, 4], [5, 4]])
    for row in rows:
        row["answer1_response_gate"] = "fail"
        change(row, score_status="gate_blocked", score=None)
    change(rows[1], applicable=False)
    coverage = record(report(snapshot, rows), "B1．", 对象="个性化适配 / 产品1", 指标="有效评分覆盖率")
    assert (coverage["数量"], coverage["分母"], coverage["占比"]) == (0, 1, 0)
    for row in rows:
        change(row, applicable=None, verification_status=None)
    tables = report(snapshot, rows)
    coverage = record(tables, "B1．", 对象="个性化适配 / 产品1", 指标="有效评分覆盖率")
    assert (coverage["数量"], coverage["分母"], coverage["占比"]) == (0, 0, None)
    assert record(tables, "B1．", 对象="个性化适配 / 产品1", 指标="适用性未知数")["数量"] == 2


def test_gate_rates_exclude_input_failed_and_keep_missing_gate_diagnostic():
    snapshot, rows, _ = dataset([[5, 4]] * 4)
    rows[1]["answer1_response_gate"] = "unclear"
    rows[2].update(answer1_input_status="failed", answer1_response_gate="unclear")
    del rows[3]["answer1_response_gate"]
    tables = report(snapshot, rows)
    gate = record(tables, "A．", 对象="产品1", 指标="响应Gate通过率")
    assert (gate["数量"], gate["分母"], gate["占比"]) == (1, 2, .5)
    observable = record(tables, "A．", 对象="产品1", 指标="响应Gate可观察覆盖率")
    assert (observable["数量"], observable["分母"]) == (2, 4)
    assert record(tables, "A．", 对象="产品1", 指标="响应Gate状态缺失")["数量"] == 1


@pytest.mark.parametrize("mutation", ["missing_revision", "unknown_revision", "identity_conflict", "unknown_family"])
def test_chat_unknown_identity_never_uses_current_or_old_policy(mutation):
    snapshot, rows, _ = dataset([[5, 4]])
    if mutation == "missing_revision":
        del snapshot["protocol_manifest"]["bundle_revision"]
        del rows[0]["bundle_revision"]
    elif mutation == "unknown_revision":
        rows[0]["bundle_revision"] = "9.0.0"
    elif mutation == "identity_conflict":
        rows[0]["standard_id"] = "qa_competitor_compare"
    else:
        snapshot["evaluation_profile"] = "unknown@1.0"
        snapshot["protocol_manifest"].update(id="unknown@1.0", standard_id="unknown")
        rows[0].update(evaluation_profile="unknown@1.0", standard_id="unknown")
    tables = report(snapshot, rows)
    assert any(t.title == "评分统计不可用" for t in tables)
    assert not any(t.title.startswith("B2．") for t in tables)


def test_chat_identity_can_recover_from_unique_complete_result_record():
    snapshot, rows, _ = dataset([[5, 4]])
    del snapshot["evaluation_profile"]
    del snapshot["protocol_manifest"]
    tables = report(snapshot, rows)
    assert not any(t.title == "评分统计不可用" for t in tables)
    assert record(tables, "B2．", 维度="个性化适配", 产品="产品1")["均分"] == 5


def test_mixed_conversations_and_independent_items_share_unit_weight():
    snapshot, rows, _ = dataset([[5, 1], [5, 1], [1, 5], [3, 3], [4, None]])
    for index, turn in ((0, 1), (1, 2), (4, 3)):
        snapshot["items"][index].update(session_id="long", turn_index=turn)
    # A reused business ID without a turn does not create a conversation.
    snapshot["items"][2]["session_id"] = snapshot["items"][3]["session_id"] = "business"
    tables = stats.conversation_statistics(snapshot, rows)
    paired = record(tables, "多轮会话等权", 维度="理解需求", 方向="1/2")
    assert (paired["有效单元数"], paired["其中独立题数"], paired["其中可靠会话数"], paired["有效配对轮数"]) == (3, 2, 1, 4)
    assert paired["A单元等权均值"] == paired["B单元等权均值"] == 3
    assert paired["会话等权分位值"] == 1
    product = record(tables, "会话与独立题等权", 维度="理解需求", 产品="产品1")
    assert product["等权均值"] == pytest.approx(26 / 9)
    assert product["有效轮数"] == 5
    assert len(table(tables, "多轮会话覆盖").rows) == 1


def test_duplicate_turns_are_isolated_while_missing_prefix_turn_is_diagnosed():
    snapshot, rows, _ = dataset([[5, 1], [1, 5]])
    for item in snapshot["items"]:
        item.update(session_id="bad", turn_index=1)
    tables = stats.conversation_statistics(snapshot, rows)
    assert all("轮次重复" in row[6] for row in tables[0].rows)
    paired = record(tables, "多轮会话等权", 维度="理解需求", 方向="1/2")
    assert paired["有效单元数"] == paired["其中独立题数"] == 2
    assert paired["其中可靠会话数"] == 0
    snapshot["items"][1]["turn_index"] = 3
    tables = stats.conversation_statistics(snapshot, rows)
    assert tables[0].rows[0][1:4] == [3, 2, False]
    assert "缺失已知轮次：2" in tables[0].rows[0][6]


def test_reliable_conversation_bootstrap_clusters_rounds_not_business_ids():
    snapshot, rows, _ = dataset([[5, 1], [1, 5], [3, 3], [4, 2]])
    for index in (0, 1):
        snapshot["items"][index].update(session_id="real", turn_index=index + 1)
    for index in (2, 3):
        snapshot["items"][index]["session_id"] = "business"
    cases = stats._cases(snapshot, rows)
    assert stats._cluster_key(cases[0]) == stats._cluster_key(cases[1])
    assert stats._cluster_key(cases[2]) != stats._cluster_key(cases[3])
    assert len({c.statistic_unit for c in cases}) == 3


def test_chat_tables_render_numeric_rates_and_decimal_statistics_without_mutation():
    snapshot, rows, _ = dataset([[5, 4], [4, 3]])
    before = deepcopy((snapshot, rows))
    tables = report(snapshot, rows)

    def column(n):
        result = ""
        while n:
            n, digit = divmod(n - 1, 26)
            result = chr(65 + digit) + result
        return result

    xml = statistics_sheet_xml(tables, style_start=2, escape_text=lambda x: escape(str(x)), column_name=column)
    root = ET.fromstring(xml)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    numeric_percent = [c for c in root.findall(".//s:c", ns) if c.get("s") == "7" and c.find("s:v", ns) is not None]
    assert numeric_percent and all(c.get("t") is None for c in numeric_percent)
    assert "个性化适配" in xml and "情感表达恰当" in xml and "风格恰当" in xml
    assert "内容准确性" not in xml and "产品3" not in xml
    assert (snapshot, rows) == before


def test_merge_retains_stable_plus_legacy_thresholds_and_dimension_set():
    from test_compare_statistics import make_snapshot, report as legacy_report

    snapshot = make_snapshot([[value, 3] for value in range(1, 6)], "0.2-simplified-plus")
    tables = legacy_report(snapshot)
    protocol = resolve_compare_protocol("qa_competitor_compare@0.2-simplified-plus")
    assert protocol.status == "stable"
    assert protocol.require_response_pass is True
    assert protocol.bundle_revision == "0.2.1"
    assert {row[0] for row in table(tables, "B2．").rows} == {
        protocol.dimension_names[key] for key in protocol.score_dimensions}
    performance = record(tables, "B2．", 维度="理解需求", 产品="产品1")
    assert performance["均分"] == performance["中位数"] == 3
    assert performance["达标率"] == performance["低质率"] == .4
    assert not any(row[0] in ("个性化适配", "情感表达恰当", "风格恰当")
                   for row in table(tables, "B2．").rows)


@pytest.mark.parametrize("gate,value,expected_pairs", [
    ("response", "pass", 1), ("response", "unclear", 0), ("response", "fail", 0),
    ("safety", "fail", 1), ("safety", "unclear", 1),
])
def test_merge_stable_plus_preserves_legacy_gate_policy(gate, value, expected_pairs):
    from test_compare_statistics import make_snapshot, report as legacy_report

    snapshot = make_snapshot([[4, 3]], "0.2-simplified-plus")
    snapshot["results"][0][f"answer1_{gate}_gate"] = value
    tables = legacy_report(snapshot)
    pair = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert pair["配对有效题数"] == expected_pairs
    # The new chat mode deliberately has different unclear/safety-fail rules.
    chat_snapshot, chat_rows, chat_protocol = dataset([[4, 3]])
    chat_rows[0][f"answer1_{gate}_gate"] = value
    chat_score = stats.score_state(stats._cases(chat_snapshot, chat_rows)[0], 1, "understanding", chat_protocol)[1]
    assert (chat_score is not None) == (value != "fail")


@pytest.mark.parametrize("standard,revisions", [
    ("0.2-simplified", ("0.2.0", "0.2.1")),
    ("0.2-simplified-plus", ("0.2.1",)),
    ("0.2-simplified-calibrated", ("0.2.2", "0.2.3")),
    ("0.2-simplified-thinking-exposure", ("0.2.3", "0.2.4")),
    ("0.3", ("0.3.0", "0.3.1")),
])
def test_merge_legacy_frozen_revisions_and_missing_revision_compatibility(standard, revisions):
    from test_compare_statistics import make_snapshot, report as legacy_report

    for revision in revisions:
        policy = stats._policy(standard, revision)
        assert policy is not None and policy.bundle_revision == revision
    assert stats._policy(standard, "9.9.9") is None
    inferred = stats._policy(standard, "未记录实现版本")
    assert inferred is not None and inferred.id == f"qa_competitor_compare@{standard}"
    snapshot = make_snapshot([[3, 2]], standard)
    del snapshot["protocol_manifest"]["bundle_revision"]
    del snapshot["results"][0]["bundle_revision"]
    tables = legacy_report(snapshot)
    assert not any(selected.title == "评分统计不可用" for selected in tables)
    assert record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == 1
    assert stats._policy("1.0", "未记录实现版本", "qa_chat_compare") is None
