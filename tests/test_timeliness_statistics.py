"""R19 denominators and intersections stay independent of legacy protocols."""
from copy import deepcopy
import json

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web.compare_statistics import _cases, build_compare_statistics, score_state
from auto_eval.web.tasks import Task
from auto_eval.web.timeliness_summary import summarize_timeliness


def dataset(scores=((3, 2),), revision=None):
    protocol = resolve_compare_protocol("timeliness_compare@1.0", revision)
    legacy = protocol.result_schema_version == "timeliness-result-1"
    if legacy:
        from auto_eval.judges.timeliness_schema_r100 import finalize_timeliness_observation
        from auto_eval.judges.visual_compare_prompt_timeliness_r100 import output_example
    elif protocol.result_schema_version == "timeliness-result-2":
        from auto_eval.judges.timeliness_schema_r101 import finalize_timeliness_observation
        from auto_eval.judges.visual_compare_prompt_timeliness_r101 import output_example
    else:
        from auto_eval.judges.timeliness_schema import finalize_timeliness_observation
        from auto_eval.judges.visual_compare_prompt_timeliness import output_example
    count = len(scores[0])
    snapshot = dict(task_id="time-test", mode="compare", evaluation_profile=protocol.id,
                    protocol_manifest=protocol.public_metadata(), items=[], results=[])
    for index, values in enumerate(scores):
        snapshot["items"].append(dict(id=f"time-{index}", query="今天几点开园？", product_count=count))
        raw = json.loads(output_example(count))
        for p, score in enumerate(values, 1):
            for gate in ("response", "safety"):
                raw[f"answer{p}_{gate}_gate_reason"] = f"产品{p}：截图当前回答可读，无严重风险"
                raw[f"answer{p}_{gate}_gate"] = "pass"
            for dim in protocol.dimensions:
                raw[f"answer{p}_{dim}_score"] = score
                if legacy:
                    raw.update({f"answer{p}_{dim}_applicable": True,
                                f"answer{p}_{dim}_verification_status": "verified" if score is not None else "unverifiable"})
                elif dim != "understanding" and score is None:
                    raw[f"answer{p}_{dim}_na_reason"] = "insufficient_evidence"
        for dim in protocol.dimensions:
            raw[dim + "_reason"] = "；".join(f"产品{p}：K1截图中来源与适用日期支持判档。" for p in range(1, count + 1))
            raw[dim + "_evidence"] = [f"产品{p}｜K1｜图1公告卡：2026年10月20日有效" for p in range(1, count + 1)]
        observation = protocol.observation_model.model_validate(raw, context={"product_count": count})
        result = finalize_timeliness_observation(observation, protocol)
        result.update(index=index, **{"评估状态": "已完成"})
        snapshot["results"].append(result)
    return snapshot, snapshot["results"], protocol


def change(row, product, dimension, **fields):
    row.update({f"answer{product}_{dimension}_{key}": value for key, value in fields.items()})


def summary(snapshot):
    return summarize_timeliness(Task("time-test", "compare", snapshot["items"], {},
        evaluation_profile=snapshot["evaluation_profile"], protocol_manifest=snapshot["protocol_manifest"],
        results=snapshot["results"]))


def test_coverage_keeps_insufficient_evidence_and_separates_unknown_applicability():
    snapshot, rows, protocol = dataset(((3, 2),) * 6)
    change(rows[1], 1, "timeliness", na_reason="insufficient_evidence", score=None, score_status="insufficient_evidence")
    change(rows[2], 1, "timeliness", applicable=False, na_reason="not_applicable", score=None, score_status="not_applicable")
    change(rows[3], 1, "timeliness", applicable=None, na_reason="applicability_unknown", score=None, score_status="insufficient_evidence")
    rows[4]["answer1_response_gate"] = "fail"
    for dim in protocol.dimensions:
        change(rows[4], 1, dim, applicable=None, verification_status=None, score=None, score_status="blocked_gate")
    change(rows[5], 1, "understanding", score=1)
    for dim in ("authority", "timeliness"):
        change(rows[5], 1, dim, applicable=None, verification_status=None, score=None, score_status="blocked_understanding")
    live = summary(snapshot)
    assert live["timeliness_answer1_scored"] == 1
    assert live["timeliness_answer1_eligible"] == 2
    assert live["timeliness_answer1_score_coverage"] == .5
    assert live["timeliness_answer1_applicable_unknown"] == 1
    assert live["timeliness_answer1_total"] == 6
    assert live["timeliness_answer1_score_status_rates"]["blocked_gate"] == pytest.approx(1 / 6)
    assert live["timeliness_answer1_score_status_rates"]["not_applicable"] == pytest.approx(1 / 6)
    assert live["timeliness_answer1_score_status_counts"] == {
        "scored": 1, "insufficient_evidence": 2, "not_applicable": 1, "blocked_gate": 1, "blocked_understanding": 1}
    assert live["timeliness_answer1_answer2_paired_count"] == 1
    assert live["timeliness_answer1_answer2_paired_answer2_avg"] == 2


def test_gate_unclear_and_understanding_na_do_not_block_other_dimensions():
    snapshot, rows, protocol = dataset()
    rows[0]["answer1_response_gate"] = "unclear"
    change(rows[0], 1, "understanding", score=None, score_status="insufficient_evidence")
    case = _cases(snapshot, rows)[0]
    assert score_state(case, 1, "authority", protocol) == ("有效评分", 3)
    assert summary(snapshot)["answer1_gate_unclear"] == 1


def test_decisive_core_one_partial_is_valid_and_no_averaging_unknowns():
    # Frozen revision 1.0.0 keeps its explicit verification contract.
    snapshot, rows, protocol = dataset(revision="1.0.0")
    change(rows[0], 1, "authority", verification_status="partial", score=1)
    assert score_state(_cases(snapshot, rows)[0], 1, "authority", protocol) == ("有效评分", 1)
    change(rows[0], 1, "authority", score=2)
    assert score_state(_cases(snapshot, rows)[0], 1, "authority", protocol) == ("评分状态冲突", None)


def test_simplified_numeric_scores_do_not_require_uncollected_verification():
    snapshot, rows, protocol = dataset()
    assert rows[0]["answer1_authority_verification_status"] is None
    assert rows[0]["answer1_authority_verification_status_source"] == "not_collected"
    for product in (1, 2):
        for dimension in protocol.dimensions:
            rows[0].pop(f"answer{product}_{dimension}_verification_status")
    assert score_state(_cases(snapshot, rows)[0], 1, "authority", protocol) == ("有效评分", 3)
    assert summary(snapshot)["authority_answer1_score_coverage"] == 1


@pytest.mark.parametrize("fields", [
    {"na_reason": "not_applicable"},
    {"applicable": False},
    {"score_status": "insufficient_evidence"},
    {"score": None},
    {"score": None, "na_reason": "not_applicable", "score_status": "not_applicable"},
    {"score": None, "na_reason": "applicability_unknown", "score_status": "insufficient_evidence"},
])
def test_simplified_scores_require_consistent_program_state_and_na_reason(fields):
    snapshot, rows, protocol = dataset()
    change(rows[0], 1, "authority", **fields)
    assert score_state(_cases(snapshot, rows)[0], 1, "authority", protocol) == ("评分状态冲突", None)


def test_simplified_understanding_null_is_eligible_without_extra_na_field():
    snapshot, rows, protocol = dataset()
    change(rows[0], 1, "understanding", score=None, score_status="insufficient_evidence")
    assert score_state(_cases(snapshot, rows)[0], 1, "understanding", protocol) == ("证据不足", None)
    live = summary(snapshot)
    assert live["understanding_answer1_eligible"] == 1
    assert live["understanding_answer1_score_coverage"] == 0
    assert live["authority_answer1_avg"] == 3


def test_input_failure_technical_failure_and_unfinished_do_not_become_quality_na():
    snapshot, rows, _ = dataset(((3, 2),) * 3)
    rows[0]["answer1_input_status"] = "failed"
    snapshot["results"] = [rows[0], dict(index=1, error="timeout")]
    live = summary(snapshot)
    assert live["input_failed"] == 1
    assert (live["done"], live["failed"], live["unfinished"]) == (1, 1, 1)
    assert live["authority_answer1_score_status_counts"] == {"input_failed": 1, "technical_failure": 1, "unfinished": 1}
    assert live["authority_answer1_eligible"] == 0
    assert live["answer1_response_gate_observed"] == 0


def test_three_product_pair_intersections_differ_from_all_product_intersection():
    snapshot, rows, _ = dataset(((3, 2, None), (3, None, 2), (2, 3, 3)))
    live = summary(snapshot)
    assert live["authority_all_products_scored"] == 1
    assert live["authority_answer1_answer2_paired_count"] == 2
    assert live["authority_answer1_answer3_paired_count"] == 2
    assert live["authority_answer2_answer3_paired_count"] == 1
    assert live["authority_answer1_all_products_avg"] == 2
    before = deepcopy(snapshot)
    tables = build_compare_statistics(snapshot, rows)
    assert all(len(row) == len(table.headers) for table in tables for row in table.rows)
    assert all(not table.formats or len(table.formats) == len(table.headers) for table in tables)
    assert not any(any(word in str(header) for word in ("胜率", "排名", "分位值", "达标")) for table in tables for header in table.headers)
    assert snapshot == before


@pytest.mark.parametrize("bad", (True, 0, 4, 2.0, "2"))
def test_historical_bad_scores_are_not_official_numbers(bad):
    snapshot, rows, protocol = dataset()
    change(rows[0], 1, "authority", score=bad)
    assert score_state(_cases(snapshot, rows)[0], 1, "authority", protocol)[1] is None


def test_all_product_summary_uses_same_maximum_product_group_for_every_mean():
    snapshot, rows, _ = dataset(((3, 3), (2, 2)))
    # The dataset helper is intentionally homogeneous; explicitly build a mixed
    # task from two independently finalized groups.
    third_snapshot, third_rows, _ = dataset(((2, 2, 2),))
    snapshot["items"][1] = third_snapshot["items"][0]
    snapshot["results"][1] = {**third_rows[0], "index": 1}
    live = summary(snapshot)
    assert live["product_count"] == 3
    assert live["authority_all_products_scored"] == 1
    assert [live[f"authority_answer{p}_all_products_avg"] for p in (1, 2, 3)] == [2, 2, 2]


def test_conversation_equal_weights_use_only_paired_turns_without_ratios():
    from auto_eval.web.compare_statistics import conversation_statistics
    snapshot, rows, _ = dataset(((3, 2), (2, 3), (3, None), (2, 2)))
    for index in range(3):
        snapshot["items"][index].update(session_id="conversation", turn_index=index + 1)
    tables = conversation_statistics(snapshot, rows)
    paired = next(t for t in tables if t.title == "时效性会话同题配对等权均值")
    data = next(dict(zip(paired.headers, row)) for row in paired.rows if row[0] == "理解需求")
    assert data["有效单元数"] == 2 and data["配对轮数"] == 3
    assert data["A单元等权均分"] == data["B单元等权均分"] == 2.25
    assert not any("分位值" in header or "胜率" in header for table in tables for header in table.headers)
    assert summary(snapshot)["conversation_statistics"]
