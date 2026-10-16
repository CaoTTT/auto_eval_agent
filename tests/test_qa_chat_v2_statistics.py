"""Balanced output statistics keep unknown collection distinct from failure."""
from copy import deepcopy

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import compare_statistics as stats
from auto_eval.web.qa_chat_summary import summarize_chat
from auto_eval.web.tasks import Task
from test_qa_chat_statistics import change, record, table


def dataset(scores):
    protocol = resolve_compare_protocol("qa_chat_compare@1.1")
    count = len(scores[0])
    snapshot = {"mode": "compare", "evaluation_profile": protocol.id,
                "protocol_manifest": protocol.public_metadata(), "items": [], "results": []}
    for index, values in enumerate(scores):
        snapshot["items"].append({"id": f"v2-{index}", "query": f"聊聊问题{index}", "product_count": count})
        row = {"index": index, "评估状态": "已完成", "standard_id": protocol.standard_id,
               "standard_version": protocol.standard_version, "evaluation_profile": protocol.id,
               "bundle_revision": protocol.bundle_revision, "result_schema_version": protocol.result_schema_version,
               "product_count": count, "trusted_collection_metadata": {}}
        for p, score in enumerate(values, 1):
            row.update({f"answer{p}_input_status": None, f"answer{p}_response_gate": "pass",
                        f"answer{p}_safety_gate": "pass", f"answer{p}_response_gate_reason": "当前展示正常",
                        f"answer{p}_safety_gate_reason": "当前需求安全，回复无明显误拒"})
            for dimension in protocol.score_dimensions:
                change(row, p, dimension, applicable=True,
                       verification_status="verified" if score is not None else "unverifiable",
                       score_status="scored" if score is not None else "unverifiable", score=score)
        for dimension in protocol.score_dimensions:
            row[f"{dimension}_reason"] = "；".join(f"产品{p}：依据当前可见回答判断该维度" for p in range(1, count + 1))
            row[f"{dimension}_evidence"] = [f"产品{p} T01 截图上部：当前回复" for p in range(1, count + 1)]
        snapshot["results"].append(row)
    return snapshot, snapshot["results"], protocol


def summary(snapshot):
    task = Task("v2-statistics", "compare", snapshot["items"], {},
                evaluation_profile=snapshot["evaluation_profile"], protocol_manifest=snapshot["protocol_manifest"],
                results=snapshot["results"])
    return summarize_chat(task)


@pytest.mark.parametrize("count", (2, 3))
def test_unknown_collection_keeps_scores_and_all_product_directions(count):
    snapshot, rows, protocol = dataset([[5, 4, 3][:count], [1, 3, 5][:count]])
    before = deepcopy(snapshot)
    cases = stats._cases(snapshot, rows)
    assert stats.balanced_chat(protocol)
    assert all(stats.score_state(case, p, "understanding", protocol)[1] is not None
               for case in cases for p in range(1, count + 1))
    tables = stats.build_compare_statistics(snapshot, rows)
    assert len(table(tables, "C1．").rows) == 6 * count * (count - 1)
    assert record(tables, "B2．", 维度="理解需求", 产品="产品1")["有效题数"] == 2
    assert record(tables, "A．", 对象="产品1", 指标="采集状态未知")["数量"] == 2
    assert record(tables, "A．", 对象="产品1", 指标="响应Gate通过率")["占比"] == 1
    assert all(len(row) == len(selected.headers) for selected in tables for row in selected.rows)
    assert all(not selected.formats or len(selected.formats) == len(selected.headers) for selected in tables)
    assert snapshot == before
    assert all(row[f"answer{p}_input_status"] is None for row in rows for p in range(1, count + 1))


@pytest.mark.parametrize("count", (2, 3))
@pytest.mark.parametrize("collection_status", (None, "failed"))
def test_real_balanced_finalizer_and_statistics_share_unknown_and_failed_policy(count, collection_status):
    from auto_eval.judges.qa_chat_v2_schema import expected_output_keys, finalize_qa_chat_v2_observation

    snapshot, rows, protocol = dataset([[5, 4, 3][:count]])
    raw = {key: rows[0][key] for key in expected_output_keys(count)}
    raw["answer1_response_gate"] = "unclear"
    trusted = {} if collection_status is None else {"answer1": {"input_status": "failed", "source": "controlled-test"}}
    if collection_status == "failed":
        raw.update(answer1_personalization_applicable=False, answer1_personalization_verification_status=None)
    observation = protocol.observation_model.model_validate(raw, context={"product_count": count, "trusted_collection_metadata": trusted})
    result = finalize_qa_chat_v2_observation(observation, protocol)
    result.update(index=0, **{"评估状态": "已完成"})
    snapshot["results"] = [result]
    case = stats._cases(snapshot, [result])[0]
    expected = ("有效评分", 5.0) if collection_status is None else ("输入失败", None)
    assert stats.score_state(case, 1, "understanding", protocol) == expected
    assert result["answer1_input_status"] == collection_status
    assert result["answer2_input_status"] is None
    live = summary(snapshot)
    assert live["understanding_answer1_avg"] == expected[1]
    assert live["understanding_answer2_avg"] == 4
    assert live["answer1_response_gate_observed"] == int(collection_status is None)


@pytest.mark.parametrize("status", (None, "complete", "partial"))
def test_v2_collection_states_and_old_v1_missing_status_are_independent(status):
    snapshot, rows, protocol = dataset([[4, 3]])
    rows[0]["answer1_input_status"] = status
    case = stats._cases(snapshot, rows)[0]
    assert stats.score_state(case, 1, "style", protocol) == ("有效评分", 4.0)
    assert stats.gate_observable(case, 1, protocol)
    old_protocol = resolve_compare_protocol("qa_chat_compare@1.0")
    assert not stats.balanced_chat(old_protocol)
    if status is None:
        assert stats.score_state(case, 1, "style", old_protocol) == ("输入状态缺失", None)
        assert not stats.gate_observable(case, 1, old_protocol)


def test_unclear_gate_and_partial_verification_are_limited_valid_scores():
    snapshot, rows, protocol = dataset([[5, 3]])
    rows[0].update(answer1_response_gate="unclear", needs_human_review=True)
    change(rows[0], 1, "personalization", verification_status="partial")
    assert stats.score_state(stats._cases(snapshot, rows)[0], 1, "personalization", protocol) == ("有效评分", 5.0)
    tables = stats.build_compare_statistics(snapshot, rows)
    product = record(tables, "B2．", 维度="个性化适配", 产品="产品1")
    assert product["有效题数"] == product["受限证据题数"] == 1
    assert record(tables, "C1．", 维度="个性化适配", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == 1


@pytest.mark.parametrize("gate", ("response", "safety"))
def test_gate_failure_only_blocks_its_product_with_unknown_collection(gate):
    snapshot, rows, protocol = dataset([[5, 4, 3]])
    rows[0][f"answer1_{gate}_gate"] = "fail"
    for dimension in protocol.score_dimensions:
        change(rows[0], 1, dimension, score_status="gate_blocked", score=None)
    # Gate has priority over the product-specific NA determination.
    change(rows[0], 1, "personalization", applicable=False, verification_status="not_required")
    case = stats._cases(snapshot, rows)[0]
    assert stats.score_state(case, 1, "personalization", protocol)[0].endswith("Gate阻断")
    assert stats.score_state(case, 2, "personalization", protocol) == ("有效评分", 4.0)
    tables = stats.build_compare_statistics(snapshot, rows)
    assert record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品2 / 产品3"})["配对有效题数"] == 1
    assert record(tables, "D．", 维度="理解需求", 产品="产品2")["三方有效题数"] == 0


def test_trusted_failed_collection_precedes_gate_and_preserves_other_products():
    snapshot, rows, protocol = dataset([[5, 4, 3]])
    rows[0].update(answer1_input_status="failed", answer1_response_gate="unclear", answer1_safety_gate="unclear")
    rows[0]["trusted_collection_metadata"] = {"answer1": {"input_status": "failed", "source": "controlled-test"}}
    for dimension in protocol.score_dimensions:
        change(rows[0], 1, dimension, score_status="input_failed", score=None)
    change(rows[0], 1, "personalization", applicable=False, verification_status=None)
    case = stats._cases(snapshot, rows)[0]
    assert stats.score_state(case, 1, "personalization", protocol) == ("输入失败", None)
    assert not stats.gate_observable(case, 1, protocol)
    live = summary(snapshot)
    assert live["understanding_answer1_avg"] is None
    assert live["understanding_answer2_avg"] == 4
    assert live["understanding_answer2_answer3_paired_count"] == 1
    assert live["answer1_response_gate_observed"] == 0
    assert live["answer1_response_gate_pass_rate"] is None
    assert live["input_failed"] == 1


def test_product_na_and_unknown_applicability_do_not_use_shared_reason_as_state():
    snapshot, rows, _ = dataset([[5, 4], [3, 2]])
    change(rows[0], 1, "personalization", applicable=False, verification_status="not_required", score_status="not_applicable", score=None)
    change(rows[1], 1, "personalization", applicable=None, verification_status="unverifiable", score_status="unverifiable", score=None)
    rows[0]["personalization_reason"] = "产品1：无个性化需求；产品2：具备相关偏好并有效适配"
    tables = stats.build_compare_statistics(snapshot, rows)
    assert record(tables, "B2．", 维度="个性化适配", 产品="产品2")["有效题数"] == 2
    assert record(tables, "B1．", 对象="个性化适配 / 产品1", 指标="适用性未知数")["数量"] == 1
    assert record(tables, "B1．", 对象="个性化适配 / 产品1", 指标="有效评分覆盖率")["占比"] is None
    assert record(tables, "C1．", 维度="个性化适配", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == 0


def test_gate_denominators_include_unknown_collection_but_exclude_failed_and_missing_gate():
    snapshot, rows, _ = dataset([[5, 4]] * 4)
    rows[1]["answer1_response_gate"] = "unclear"
    rows[2].update(answer1_input_status="failed", answer1_response_gate="unclear")
    del rows[3]["answer1_response_gate"]
    tables = stats.build_compare_statistics(snapshot, rows)
    rate = record(tables, "A．", 对象="产品1", 指标="响应Gate通过率")
    assert (rate["数量"], rate["分母"], rate["占比"]) == (1, 2, .5)
    assert record(tables, "A．", 对象="产品1", 指标="响应Gate状态缺失")["数量"] == 1
    assert record(tables, "A．", 对象="产品1", 指标="采集状态未知")["数量"] == 3
    pair = record(tables, "C4．", 对象="产品1 / 产品2 响应Gate", 指标="双方通过")
    assert pair["数量"] == 1 and pair["分母"] == 3
    live = summary(snapshot)
    assert live["answer1_response_gate_pass"] == 1
    assert live["answer1_response_gate_observed"] == 2
    assert live["answer1_response_gate_pass_rate"] == .5
    assert live["answer1_input_unknown"] == 3


def test_uncollected_conflict_and_confidence_are_hidden_even_if_stale_fields_exist():
    snapshot, rows, _ = dataset([[5, 4]])
    rows[0].update(has_conflict="unclear", conflict_reason="stale", confidence="high", rationale="stale")
    overview = table(stats.build_compare_statistics(snapshot, rows), "A．")
    assert not any(row[0] == "内容冲突" or "置信度" in row[1] for row in overview.rows)
    live = summary(snapshot)
    assert not any(key.startswith("conflict_") for key in live)
    assert all(not any(key.startswith("conflict_") for key in subset) for subset in live["by_input_modality"].values())
    from test_qa_chat_statistics import dataset as v1_dataset
    old_snapshot, old_rows, _ = v1_dataset([[5, 4]])
    old_overview = table(stats.build_compare_statistics(old_snapshot, old_rows), "A．")
    assert any(row[0] == "内容冲突" for row in old_overview.rows)
    assert any(row[1] == "置信度缺失" for row in old_overview.rows)


def test_summary_uses_latest_result_and_keeps_failure_unfinished_and_intersections():
    snapshot, rows, _ = dataset([[5, 3, None], [1, 3, 4], [4, None, 4]])
    # Previous attempts and a cached summary cannot change the current sample.
    snapshot["results"] = [{"index": 0, "error": "old attempt"}, *rows]
    snapshot["summary"] = {"understanding_answer1_avg": 999}
    live = summary(snapshot)
    assert (live["done"], live["failed"], live["unfinished"]) == (3, 0, 0)
    assert live["understanding_answer1_avg"] == pytest.approx(10 / 3, abs=.001)
    assert live["understanding_answer1_answer2_paired_count"] == 2
    assert live["understanding_answer1_answer3_paired_count"] == 2
    assert live["understanding_answer2_answer3_paired_count"] == live["understanding_all_products_scored"] == 1
    assert live["comparable"] == 3
    snapshot["results"] = [rows[0], {"index": 1, "error": "format error"}]
    live = summary(snapshot)
    assert (live["done"], live["failed"], live["unfinished"]) == (1, 1, 1)


def test_conversation_weighting_uses_paired_turns_with_unknown_collection():
    snapshot, rows, _ = dataset([[5, 3], [1, 3], [5, None], [1, 3], [4, 2]])
    for index, turn in ((0, 1), (1, 2), (2, 3)):
        snapshot["items"][index].update(session_id="long", turn_index=turn)
    snapshot["items"][3].update(session_id="short", turn_index=1)
    snapshot["items"][4]["session_id"] = "business-id"
    tables = stats.conversation_statistics(snapshot, rows)
    pair = record(tables, "多轮会话等权", 维度="理解需求", 方向="1/2")
    assert pair["有效单元数"] == 3 and pair["有效配对轮数"] == 4
    assert pair["A单元等权均值"] == pair["B单元等权均值"] == pytest.approx(8 / 3)
    assert pair["会话等权分位值"] == 1


@pytest.mark.parametrize("identity", ("missing_revision", "unknown_revision", "mixed_revision"))
def test_unknown_balanced_identity_still_blocks_official_score_statistics(identity):
    snapshot, rows, _ = dataset([[5, 4]])
    if identity == "missing_revision":
        del snapshot["protocol_manifest"]["bundle_revision"]
        del rows[0]["bundle_revision"]
    elif identity == "unknown_revision":
        snapshot["protocol_manifest"]["bundle_revision"] = rows[0]["bundle_revision"] = "9.9.9"
    else:
        rows[0]["bundle_revision"] = "1.0.0"
    tables = stats.build_compare_statistics(snapshot, rows)
    assert any(selected.title == "评分统计不可用" for selected in tables)
    assert not any(selected.title.startswith("B2．") for selected in tables)
    live = summary(snapshot)
    assert live["protocol_diagnostics"]
    assert "understanding_answer1_avg" not in live
