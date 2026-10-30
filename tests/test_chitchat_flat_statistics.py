"""Flat-chat independent scores survive summaries, restored history and exports."""
from copy import deepcopy
from io import BytesIO

import pytest
from openpyxl import load_workbook

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web.chitchat_flat_statistics import (
    DIMENSIONS, chitchat_flat_conversation_statistics, chitchat_flat_score_state,
)
from auto_eval.web.compare_statistics import Case, build_compare_statistics, score_state
from auto_eval.web.history import build_xlsx, export_rows, result_export_row, snapshot_payload
from auto_eval.web.protocol_identity import recover_protocol_identity
from auto_eval.web.runner import _summarize
from auto_eval.web.tasks import Task, _task_from_snapshot

PROFILE = "chitchat_evidence_first_flat@1.1"


def dataset(scores=((3, 5),)):
    protocol = resolve_compare_protocol(PROFILE)
    count = len(scores[0])
    snapshot = dict(task_id="flat-chat-test", mode="compare", evaluation_profile=protocol.id,
                    protocol_manifest=protocol.public_metadata(), items=[], results=[], options={})
    for index, values in enumerate(scores):
        snapshot["items"].append(dict(id=f"flat-{index}", query="我想听你讲，自己少说。", product_count=count))
        row = dict(index=index, item_id=f"flat-{index}", product_count=count,
                   standard_id=protocol.standard_id, standard_version=protocol.standard_version,
                   evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
                   result_schema_version=protocol.result_schema_version, **{"评估状态": "已完成"})
        for dimension in DIMENSIONS:
            row[dimension + "_reason"] = "；".join(f"产品{p}：已提供本维具体证据及其影响。" for p in range(1, count + 1))
            for product, value in enumerate(values, 1):
                score = value if dimension == "understanding" else 5
                row[f"answer{product}_{dimension}_score"] = score
                row[f"answer{product}_{dimension}_score_status"] = "unverifiable" if score is None else "scored"
        snapshot["results"].append(row)
    return snapshot


def summarize(snapshot):
    return _summarize(Task("flat-chat-test", "compare", snapshot["items"], snapshot["options"],
        evaluation_profile=PROFILE, protocol_manifest=snapshot["protocol_manifest"], results=snapshot["results"]))


def test_own_means_and_paired_means_have_exact_separate_ratios():
    snapshot = dataset(((1, 5), (3, 4), (5, None), (None, 2)))
    live = summarize(snapshot)
    assert live["understanding_answer1_avg"] == 3
    assert live["understanding_answer2_avg"] == pytest.approx(11 / 3)
    assert live["understanding_answer1_answer2_mean_ratio"] == pytest.approx(9 / 11)
    assert live["understanding_answer1_answer2_paired_mean_ratio"] == pytest.approx(4 / 9)
    assert live["understanding_answer1_answer2_paired_count"] == 2
    assert live["understanding_answer1_score_coverage"] == .75
    tables = build_compare_statistics(snapshot, snapshot["results"])
    pair = next(t for t in tables if t.title == "同题同维共同可评分交集")
    row = next(r for r in pair.rows if r[0] == "理解需求")
    assert row[3:] == [2, 2, 4.5, -2.5, 4 / 9]
    own = next(t for t in tables if t.title == "各产品有效均分比")
    assert next(r for r in own.rows if r[0] == "理解需求")[-1] == pytest.approx(9 / 11)
    assert not any("Gate" in t.title or "总分" in t.title for t in tables)
    assert all(len(r) == len(t.headers) for t in tables for r in t.rows)


def test_low_response_safety_and_understanding_never_clear_other_scores():
    snapshot = dataset(((1, 5),))
    row = snapshot["results"][0]
    row.update(answer1_response_experience_score=1, answer1_safety_stability_score=1)
    live = summarize(snapshot)
    assert live["response_experience_answer1_avg"] == 1
    assert live["safety_stability_answer1_avg"] == 1
    assert live["understanding_answer1_avg"] == 1
    assert live["style_answer1_avg"] == 5


@pytest.mark.parametrize("dimension,score", [("response_experience", 3), ("safety_stability", 4),
    ("understanding", True), ("understanding", "5"), ("understanding", 5.0), ("understanding", 0)])
def test_malformed_saved_scores_are_excluded_without_coercion(dimension, score):
    snapshot = dataset()
    row = snapshot["results"][0]
    row[f"answer1_{dimension}_score"] = score
    protocol = resolve_compare_protocol(PROFILE)
    case = Case(0, {}, row, "done", 2, protocol.standard_version, protocol.bundle_revision, protocol.standard_id)
    assert chitchat_flat_score_state(case, 1, dimension, protocol) == ("invalid_state", None)
    assert score_state(case, 1, dimension, protocol) == ("评分状态冲突", None)


def test_three_product_pair_intersections_remain_distinct():
    live = summarize(dataset(((1, 3, 5), (3, 4, None), (5, 2, 1))))
    assert live["understanding_answer1_answer2_paired_count"] == 3
    assert live["understanding_answer1_answer3_paired_count"] == 2
    assert live["understanding_all_products_scored"] == 2
    assert live["understanding_answer3_answer1_mean_ratio"] == 1


def test_all_export_routes_preserve_seven_reasons_before_numeric_scores():
    snapshot = dataset()
    result = snapshot["results"][0]
    exported = export_rows(snapshot)["逐题结果"][0]
    single = result_export_row("compare", result, 0, snapshot["items"], PROFILE,
                               snapshot["protocol_manifest"], snapshot["results"])
    assert exported == single
    for dimension, name in resolve_compare_protocol(PROFILE).dimension_names.items():
        key = name + "理由（含证据与分档依据）"
        assert exported[key] == result[dimension + "_reason"]
        assert list(exported).index(key) < list(exported).index("产品1" + name + "分")
    assert not any("Gate" in k or "个性化" in k or "总分" in k for k in exported)
    book = load_workbook(BytesIO(build_xlsx(snapshot, include_images=False)), data_only=True)
    headers, values = list(book["逐题结果"].values)[:2]
    assert values[headers.index("产品1理解需求分")] == 3
    assert values[headers.index("产品2理解需求分")] == 5
    book.close()


@pytest.mark.parametrize("missing", ["bundle_revision", "result_schema_version"])
def test_missing_frozen_identity_cannot_restore_or_export_formal_scores(missing):
    snapshot = dataset()
    snapshot["protocol_manifest"].pop(missing)
    snapshot["results"][0].pop(missing)
    snapshot["summary"] = {"understanding_answer1_avg": 5}
    recovered = recover_protocol_identity(snapshot)
    assert recovered["is_flat_chat"] and not recovered["executable"]
    public = snapshot_payload(snapshot)
    assert public["raw_summary"] == snapshot["summary"]
    assert "understanding_answer1_avg" not in public["summary"]
    assert export_rows(snapshot)["逐题结果"][0]["产品1理解需求分"] is None


def test_frozen_identity_roundtrip_and_result_input_fingerprint():
    snapshot = dataset()
    restored = _task_from_snapshot(snapshot, "flat-chat-test")
    assert restored.protocol_manifest["result_schema_version"] == "chitchat-flat-result-1"
    snapshot["items"][0].update(query_images=["synthetic.png"], input_manifest_sha256="old")
    snapshot["results"][0]["input_manifest_sha256"] = "bound-result"
    assert export_rows(snapshot)["逐题结果"][0]["输入指纹"] == "bound-result"


def test_conversation_means_are_equal_weight_per_session():
    snapshot = dataset(((1, 5), (3, 5), (5, 1)))
    for index, item in enumerate(snapshot["items"][:2], 1):
        item.update(session_id="one", turn_index=index, screenshot_scope="current_turn")
    tables = chitchat_flat_conversation_statistics(snapshot, snapshot["results"])
    product = next(t for t in tables if t.title == "闲聊七维会话与独立题等权均值")
    row = next(r for r in product.rows if r[0] == "理解需求" and r[2] == "产品1")
    assert row[3:] == [2, 3, 3.5]


def test_failed_capture_is_local_and_not_a_gate_failure():
    snapshot = dataset()
    snapshot["results"][0]["answer1_input_status"] = "failed"
    live = summarize(snapshot)
    assert live["understanding_answer1_avg"] is None
    assert live["understanding_answer2_avg"] == 5
    assert live["understanding_answer1_score_status_counts"] == {"input_failed": 1}
