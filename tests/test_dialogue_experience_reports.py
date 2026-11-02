"""Overall-score reports preserve four original fields and paired denominators."""
from io import BytesIO

from openpyxl import load_workbook
import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web.compare_statistics import Case, build_compare_statistics, score_state
from auto_eval.web.dialogue_experience_statistics import (
    dialogue_experience_conversation_statistics, dialogue_experience_score_state,
)
from auto_eval.web.history import build_xlsx, export_rows, result_export_row, snapshot_payload
from auto_eval.web.protocol_identity import recover_protocol_identity
from auto_eval.web.runner import _summarize
from auto_eval.web.tasks import Task

PROFILE = "dialogue_experience_compare@1.0"


def dataset(scores=((3, 5),)):
    protocol = resolve_compare_protocol(PROFILE)
    count = len(scores[0])
    snapshot = dict(task_id="experience-test", mode="compare", evaluation_profile=protocol.id,
                    protocol_manifest=protocol.public_metadata(), items=[], results=[], options={})
    for index, values in enumerate(scores):
        item = dict(id=f"experience-{index}", query="帮我设明早7点闹钟", product_count=count)
        row = dict(index=index, item_id=item["id"], product_count=count,
                   standard_id=protocol.standard_id, standard_version=protocol.standard_version,
                   evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
                   result_schema_version=protocol.result_schema_version, **{"评估状态": "已完成"})
        raw_products = {}
        reasons = []
        for product, value in enumerate(values, 1):
            reason = f"【类型：任务型；目标：设置闹钟；主查：执行；证据状态：不足】产品{product}材料不全，保留暂评。"
            raw_products[f"answer{product}"] = dict(score=value, reason=reason, suggestion="补充执行反馈", domain="闹钟提醒")
            item[f"context{product}"] = f"产品{product}测试背景"
            item[f"answer{product}"] = f"产品{product}复制文本"
            row[f"answer{product}_overall_score"] = value
            row[f"answer{product}_overall_reason"] = reason
            row[f"answer{product}_overall_score_status"] = "unverifiable" if value is None else "scored"
            row[f"answer{product}_overall_applicable"] = True
            row[f"answer{product}_suggestion"] = "补充执行反馈"
            row[f"answer{product}_domain"] = "闹钟提醒"
            reasons.append(reason)
        row.update(model_product_results=raw_products, overall_reason="；".join(reasons))
        snapshot["items"].append(item)
        snapshot["results"].append(row)
    return snapshot


def summarize(snapshot):
    return _summarize(Task("experience-test", "compare", snapshot["items"], snapshot["options"],
        evaluation_profile=PROFILE, protocol_manifest=snapshot["protocol_manifest"], results=snapshot["results"]))


def test_tentative_scores_survive_without_gates_or_separate_verification():
    snapshot = dataset(((2, 5),))
    summary = summarize(snapshot)
    assert summary["overall_answer1_avg"] == 2
    assert summary["overall_answer2_avg"] == 5
    assert summary["overall_answer1_low_rate"] == 1
    assert summary["overall_answer2_high_rate"] == 1
    assert not any("response_gate" in key or "safety_gate" in key for key in summary)


def test_paired_scores_use_the_same_intersection_and_all_directions():
    snapshot = dataset(((1, 5), (3, 4), (5, None), (None, 2)))
    summary = summarize(snapshot)
    assert summary["overall_answer1_avg"] == 3
    assert summary["overall_answer2_avg"] == pytest.approx(11 / 3)
    assert summary["overall_answer1_score_coverage"] == .75
    assert summary["overall_answer1_answer2_mean_ratio"] == pytest.approx(9 / 11)
    assert summary["overall_answer1_answer2_paired_mean_ratio"] == pytest.approx(4 / 9)
    assert summary["overall_answer1_answer2_paired_count"] == 2
    assert summary["overall_answer1_answer2_loss"] == 2
    assert summary["overall_answer2_answer1_win"] == 2
    tables = build_compare_statistics(snapshot, snapshot["results"])
    paired = next(table for table in tables if table.title == "同题同维共同可评分交集")
    assert paired.rows[0][3:] == [2, 2, 4.5, -2.5, 4 / 9]
    assert all(len(row) == len(table.headers) for table in tables for row in table.rows)
    assert not any("Gate" in table.title for table in tables)


def test_three_product_pair_and_rank_denominators_are_distinct():
    snapshot = dataset(((1, 3, 5), (3, 4, None), (5, 2, 1)))
    summary = summarize(snapshot)
    assert summary["overall_answer1_answer2_paired_count"] == 3
    assert summary["overall_answer1_answer3_paired_count"] == 2
    assert summary["overall_all_products_scored"] == 2
    tables = build_compare_statistics(snapshot, snapshot["results"])
    ranks = next(table for table in tables if table.title == "三产品综合体验同题排序")
    assert ranks.rows[0][1:] == [2, 1, 0, 0, 1, .5]
    gsb = next(table for table in tables if table.title == "综合体验同题胜平负")
    assert len(gsb.rows) == 6


@pytest.mark.parametrize("value", [True, "5", 5.0, 0, 6])
def test_malformed_saved_scores_never_coerce_into_valid_scores(value):
    snapshot = dataset()
    row = snapshot["results"][0]
    row["answer1_overall_score"] = value
    protocol = resolve_compare_protocol(PROFILE)
    case = Case(0, {}, row, "done", 2, protocol.standard_version, protocol.bundle_revision, protocol.standard_id)
    assert dialogue_experience_score_state(case, 1, "overall", protocol) == ("invalid_state", None)
    assert score_state(case, 1, "overall", protocol) == ("评分状态冲突", None)


def test_trusted_capture_failure_excludes_only_matching_product():
    snapshot = dataset()
    snapshot["results"][0]["answer1_input_status"] = "failed"
    summary = summarize(snapshot)
    assert summary["overall_answer1_avg"] is None
    assert summary["overall_answer2_avg"] == 5
    assert summary["overall_answer1_score_status_counts"] == {"input_failed": 1}


def test_export_routes_preserve_per_product_reason_suggestion_domain_and_text():
    snapshot = dataset(((3, 4, 5),))
    result = snapshot["results"][0]
    exported = export_rows(snapshot)["逐题结果"][0]
    single = result_export_row("compare", result, 0, snapshot["items"], PROFILE,
                               snapshot["protocol_manifest"], snapshot["results"])
    assert exported == single
    for product in (1, 2, 3):
        assert exported[f"产品{product}综合体验理由"] == result[f"answer{product}_overall_reason"]
        assert exported[f"产品{product}综合体验分"] == product + 2
        assert exported[f"产品{product}提升建议"] == "补充执行反馈"
        assert exported[f"产品{product}垂域"] == "闹钟提醒"
        assert exported[f"产品{product}回答"] == f"产品{product}复制文本"
    assert not any("Gate" in key or "理解需求分" in key or "服务闭环分" in key for key in exported)
    book = load_workbook(BytesIO(build_xlsx(snapshot, include_images=False)), data_only=True)
    headers, values = list(book["逐题结果"].values)[:2]
    assert values[headers.index("产品3综合体验分")] == 5
    assert values[headers.index("产品2垂域")] == "闹钟提醒"
    book.close()


def test_multiturn_session_means_are_equal_weight_and_keep_single_question_units():
    snapshot = dataset(((1, 5), (3, 5), (5, 1)))
    for index, item in enumerate(snapshot["items"][:2], 1):
        item.update(session_id="one", turn_index=index, screenshot_scope="current_turn")
    tables = dialogue_experience_conversation_statistics(snapshot, snapshot["results"])
    product = next(table for table in tables if table.title == "对话体验会话与独立题等权均值")
    row = next(row for row in product.rows if row[0] == "综合体验" and row[2] == "产品1")
    assert row[3:] == [2, 3, 3.5]


@pytest.mark.parametrize("missing", ["bundle_revision", "result_schema_version"])
def test_missing_frozen_identity_keeps_raw_results_but_excludes_formal_statistics(missing):
    snapshot = dataset()
    snapshot["protocol_manifest"].pop(missing)
    snapshot["results"][0].pop(missing)
    snapshot["summary"] = {"overall_answer1_avg": 5}
    recovered = recover_protocol_identity(snapshot)
    assert recovered["is_dialogue_experience"] and not recovered["executable"]
    public = snapshot_payload(snapshot)
    assert public["raw_summary"] == snapshot["summary"]
    assert "overall_answer1_avg" not in public["summary"]
    assert export_rows(snapshot)["逐题结果"][0]["产品1综合体验分"] is None
