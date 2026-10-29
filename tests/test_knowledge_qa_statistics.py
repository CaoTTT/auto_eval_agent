"""Knowledge QA report boundaries, including true zero and accuracy half marks."""
import io

import pytest
from openpyxl import load_workbook

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges.knowledge_qa_schema import KNOWLEDGE_QA_DIMENSIONS
from auto_eval.web.compare_statistics import Case, _cases, build_compare_statistics, score_state
from auto_eval.web.history import build_xlsx, export_rows, result_export_row, snapshot_payload
from auto_eval.web.knowledge_qa_export import knowledge_qa_export_rows
from auto_eval.web.knowledge_qa_statistics import (
    dimension_metrics, knowledge_qa_conversation_statistics, knowledge_qa_score_state,
)
from auto_eval.web.knowledge_qa_summary import summarize_knowledge_qa
from auto_eval.web.tasks import Task


def case(score=2, status="scored", dimension="accuracy"):
    row = {dimension + "_reason": "产品1：截图顶部有可核对的结论，满足相应分档；产品2：独立判断。",
           f"answer1_{dimension}_status": status, f"answer1_{dimension}_score_status": status,
           f"answer1_{dimension}_score": score, "result_schema_version": "knowledge-qa-result-2"}
    return Case(0, {}, row, "done", 2, "1.0", "1.0.0", "knowledge_qa_compare")


def dataset(scores=((0, 1.5),)):
    protocol = resolve_compare_protocol("knowledge_qa_compare@1.0")
    count = len(scores[0])
    snapshot = dict(task_id="knowledge-test", mode="compare", evaluation_profile=protocol.id,
                    protocol_manifest=protocol.public_metadata(), items=[], results=[],
                    options={"product_names": ["产品甲", "产品乙", "产品丙"]})
    for index, values in enumerate(scores):
        snapshot["items"].append(dict(id=f"knowledge-{index}", query="一个小时等于多少分钟？", product_count=count,
                                      context1="无额外背景", context2="无额外背景"))
        row = dict(index=index, item_id=f"knowledge-{index}", product_count=count,
                   standard_id=protocol.standard_id, standard_version=protocol.standard_version,
                   evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
                   result_schema_version=protocol.result_schema_version, **{"评估状态": "已完成"})
        for dimension in KNOWLEDGE_QA_DIMENSIONS:
            row[dimension + "_reason"] = "；".join(f"产品{p}：截图顶部的短原话支撑当前判断，按本维标准给分。" for p in range(1, count + 1))
            for product, value in enumerate(values, 1):
                prefix = f"answer{product}_{dimension}"
                row[prefix + "_score"] = value if dimension == "accuracy" else 2
                row[prefix + "_status"] = row[prefix + "_score_status"] = "scored"
        snapshot["results"].append(row)
    return snapshot, snapshot["results"], protocol


def change(row, product, dimension, score, status):
    row.update({f"answer{product}_{dimension}_score": score,
                f"answer{product}_{dimension}_status": status,
                f"answer{product}_{dimension}_score_status": status})


def summary(snapshot, subsets=False):
    return summarize_knowledge_qa(Task("knowledge-test", "compare", snapshot["items"], snapshot.get("options", {}),
        evaluation_profile=snapshot["evaluation_profile"], protocol_manifest=snapshot["protocol_manifest"],
        results=snapshot["results"]), include_subsets=subsets)


@pytest.mark.parametrize("score", [0, 1, 1.5, 2])
def test_accuracy_preserves_allowed_numeric_values(score):
    item = case(score)
    state, value = knowledge_qa_score_state(item, 1, "accuracy")
    assert state == "scored" and value == score and type(value) is type(score)


@pytest.mark.parametrize("score", [True, False, "0", "1.5", -1, 3, 0.5, 0.0, 2.0, float("nan"), float("inf")])
def test_malformed_history_cannot_become_valid_scoring(score):
    assert knowledge_qa_score_state(case(score), 1, "accuracy") == ("invalid_state", None)


def test_quality_dimensions_reject_half_marks_and_nonmedia_not_applicable():
    assert knowledge_qa_score_state(case(1.5, dimension="content_quality"), 1, "content_quality") == ("invalid_state", None)
    assert knowledge_qa_score_state(case(None, "not_applicable"), 1, "accuracy") == ("invalid_state", None)
    assert knowledge_qa_score_state(case(None, "not_applicable", "multimedia_value"), 1, "multimedia_value") == ("not_applicable", None)


def test_score_status_conflicts_and_missing_reason_are_not_upgraded():
    item = case()
    item.result["answer1_accuracy_score_status"] = "unverifiable"
    assert knowledge_qa_score_state(item, 1, "accuracy") == ("invalid_state", None)
    item = case()
    item.result["accuracy_reason"] = " "
    assert knowledge_qa_score_state(item, 1, "accuracy") == ("invalid_state", None)


def test_legacy_gates_never_block_a_valid_knowledge_score():
    item = case(0)
    item.result.update(answer1_response_gate="fail", answer1_safety_gate="fail")
    assert knowledge_qa_score_state(item, 1, "accuracy") == ("scored", 0)


def test_coverage_and_distribution_keep_unknown_failed_and_unfinished_gaps():
    cases = [case(0), case(1.5), case(None, "insufficient_evidence"), case(None, "unverifiable"), case()]
    cases[-1].status = "unfinished"
    metrics = dimension_metrics(cases, 1, "accuracy")
    assert metrics["avg"] == .75 and metrics["scored"] == 2
    assert metrics["eligible"] == 5 and metrics["score_coverage"] == .4
    assert metrics["score_distribution"] == {"0": 1, "1": 0, "1.5": 1, "2": 0}
    media = [case(None, "not_applicable", "multimedia_value"), case(0, dimension="multimedia_value")]
    metrics = dimension_metrics(media, 1, "multimedia_value")
    assert metrics["total"] == 2 and metrics["eligible"] == 1 and metrics["score_coverage"] == 1


def test_trusted_input_failure_wins_without_becoming_zero():
    item = case(1.5)
    item.result["answer1_input_status"] = "failed"
    assert knowledge_qa_score_state(item, 1, "accuracy") == ("input_failed", None)


def test_snapshot_summary_and_report_use_the_same_fractional_pair_intersection():
    snapshot, rows, protocol = dataset(((0, 2), (1.5, 0), (2, 1.5), (2, 2)))
    change(rows[2], 1, "accuracy", None, "unverifiable")
    change(rows[3], 2, "accuracy", None, "insufficient_evidence")
    live = summary(snapshot, subsets=True)
    assert live["accuracy_answer1_avg"] == pytest.approx(1.167)
    assert live["accuracy_answer1_score_distribution"] == {"0": 1, "1": 0, "1.5": 1, "2": 1}
    assert live["accuracy_answer1_answer2_paired_count"] == 2
    assert live["accuracy_answer1_answer2_paired_answer1_avg"] == .75
    assert live["accuracy_answer1_answer2_paired_answer2_avg"] == 1
    cases = _cases(snapshot, rows)
    assert score_state(cases[0], 1, "accuracy", protocol) == ("有效评分", 0)
    tables = build_compare_statistics(snapshot, rows)
    pair = next(table for table in tables if table.title == "同题同维共同可评分交集")
    first = pair.rows[0]
    assert first[3:] == [2, .75, 1, -.25]
    assert not any("Gate" in table.title or "胜率" in table.title or "总分" in table.title for table in tables)


def test_three_product_common_intersection_does_not_replace_each_pair():
    snapshot, rows, _ = dataset(((0, 1.5, 2), (1.5, 1, 2), (2, 0, 1)))
    change(rows[1], 3, "accuracy", None, "unverifiable")
    live = summary(snapshot)
    assert live["accuracy_answer1_answer2_paired_count"] == 3
    assert live["accuracy_answer1_answer3_paired_count"] == 2
    assert live["accuracy_all_products_scored"] == 2
    assert live["accuracy_answer1_all_products_avg"] == 1


def test_export_merges_evidence_and_reason_and_keeps_scores_numeric():
    snapshot, rows, _ = dataset()
    exported = knowledge_qa_export_rows(snapshot, rows)[0]
    assert exported["产品1准确性分"] == 0
    assert exported["产品2准确性分"] == 1.5
    assert exported["准确性理由（含证据与分档依据）"] == rows[0]["accuracy_reason"]
    assert not any("Gate" in key or "证据（维度共享）" in key for key in exported)
    public = result_export_row("compare", rows[0], 0, snapshot["items"],
                               evaluation_profile=snapshot["evaluation_profile"],
                               protocol_manifest=snapshot["protocol_manifest"], task_results=rows)
    assert public["产品1准确性分"] == 0 and public["产品2准确性分"] == 1.5
    routed = export_rows(snapshot)["逐题结果"][0]
    assert routed["产品1准确性分"] == 0 and routed["产品2准确性分"] == 1.5
    workbook = load_workbook(io.BytesIO(build_xlsx(snapshot, include_images=False)), data_only=True)
    data = list(workbook["逐题结果"].values)
    labels, values = data[0], data[1]
    assert values[labels.index("产品1准确性分")] == 0
    assert values[labels.index("产品2准确性分")] == 1.5


def test_identity_conflict_preserves_raw_export_instead_of_scoring():
    snapshot, rows, _ = dataset()
    rows[0]["bundle_revision"] = "unrecorded-version"
    tables = build_compare_statistics(snapshot, rows)
    assert any(table.title == "评分统计不可用" for table in tables)
    exported = export_rows(snapshot)["逐题结果"][0]
    assert exported["产品1准确性分"] is None
    assert exported["原始结果（协议待恢复）"]["answer1_accuracy_score"] == 0


def test_vqa_export_uses_bound_result_fingerprint_and_original_query_images():
    snapshot, rows, _ = dataset()
    snapshot["items"][0].update(query_images=["/existing/query.png"], input_manifest_sha256="old-input")
    rows[0]["input_manifest_sha256"] = "bound-result-input"
    exported = export_rows(snapshot)
    assert exported["逐题结果"][0]["输入指纹"] == "bound-result-input"
    assert exported["逐题结果"][0]["提问图片"] == ["/existing/query.png"]
    assert exported["提问图片清单"][0]["image_role"] == "query_image"


def test_unresolved_history_exposes_diagnostics_and_retains_raw_summary():
    snapshot, rows, _ = dataset()
    snapshot["protocol_manifest"].pop("result_schema_version")
    rows[0].pop("result_schema_version")
    snapshot["summary"] = {"total": 1, "done": 1, "accuracy_answer1_avg": 2}
    public = snapshot_payload(snapshot)
    assert public["protocol_executable"] is False
    assert public["protocol_diagnostics"]
    assert "accuracy_answer1_avg" not in public["summary"]
    assert public["raw_summary"]["accuracy_answer1_avg"] == 2
    assert "原始汇总（协议待恢复）" in export_rows(snapshot)


def test_multiturn_session_means_keep_half_marks_and_equal_session_weights():
    snapshot, rows, _ = dataset(((0, 2), (1.5, 2), (2, 0)))
    for index, item in enumerate(snapshot["items"][:2], 1):
        item.update(session_id="session-1", turn_index=index, screenshot_scope="current_turn")
    tables = knowledge_qa_conversation_statistics(snapshot, rows)
    product_table = next(table for table in tables if table.title == "知识问答会话与独立题等权均值")
    assert product_table.rows[0][3:] == [2, 3, 1.375]
    pair_table = next(table for table in tables if table.title == "知识问答会话同题配对等权均值")
    assert pair_table.rows[0][4:] == [2, 3, 1.375, 1]
