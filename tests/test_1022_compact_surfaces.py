"""Offline checks for compact 1022 reporting and frozen baseline alignment."""
from copy import deepcopy
from io import BytesIO

from openpyxl import load_workbook
import pytest

from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID, resolve_compare_protocol
from auto_eval.web import compare_statistics as stats, history
from auto_eval.web.human_baselines import HumanError
from auto_eval.web.human_compare import align_baseline_to_run
from test_human_compare import example


def compact_protocol(revision="0.2.3"):
    return resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, revision)


def compact_result(count=2):
    policy = compact_protocol()
    row = dict(index=0, item_id="case-0", query="问题0", product_count=count,
               standard_id=policy.standard_id, standard_version=policy.standard_version,
               evaluation_profile=policy.id, bundle_revision=policy.bundle_revision,
               output_contract=policy.output_contract, content_conflict="no", has_conflict="no",
               conflict_reason="各产品的可见内容没有相互冲突。")
    for n in range(1, count + 1):
        row.update({f"answer{n}_input_status": "complete", f"answer{n}_response_gate": "pass",
                    f"answer{n}_safety_gate": "pass", f"answer{n}_response_gate_reason": "正常响应",
                    f"answer{n}_safety_gate_reason": "无可见安全问题"})
    for dimension in policy.dimensions:
        row[f"{dimension}_applicable"] = True
        row[f"{dimension}_reason"] = "；".join(f"产品{n}：首屏包含主要要求，信息明确，支持用户继续操作。" for n in range(1, count + 1))
        row.update({f"answer{n}_{dimension}_score": 5 - n for n in range(1, count + 1)})
    return row


def compact_case(row=None):
    row = compact_result() if row is None else row
    return stats.Case(0, {}, row, "done", row["product_count"], "0.2-simplified-1022", row["bundle_revision"])


def compact_snapshot(count=2):
    return dict(mode="compare", status="done", task_id="compact-offline", dataset_name="offline",
                evaluation_profile=V1022_COMPARE_PROTOCOL_ID, protocol_manifest=compact_protocol().public_metadata(),
                items=[dict(id="case-0", query="问题0", product_count=count)], results=[compact_result(count)])


def test_scores_need_no_removed_verification_and_do_not_invent_product_three():
    case = compact_case()
    assert stats.score_state(case, 1, "understanding", compact_protocol()) == ("有效评分", 4)
    assert stats.score_state(case, 3, "understanding", compact_protocol()) == ("非活动产品", None)
    assert stats.score_state(case, 1, "accuracy", compact_protocol()) == ("未采集维度", None)


@pytest.mark.parametrize("gate", ["response", "safety"])
@pytest.mark.parametrize("state", ["fail", "unclear"])
def test_either_gate_blocks_even_if_dimensions_are_omitted(gate, state):
    row = compact_result()
    row[f"answer1_{gate}_gate"] = state
    for dimension in compact_protocol().dimensions:
        row.pop(f"{dimension}_applicable")
        row.pop(f"answer1_{dimension}_score")
        assert stats.score_state(compact_case(row), 1, dimension, compact_protocol()) == (
            "响应Gate阻断" if gate == "response" else "安全Gate阻断", None)


@pytest.mark.parametrize("value", [True, "4", 4.5, 0, 6])
def test_compact_scores_are_strict_integers(value):
    row = compact_result()
    row["answer1_understanding_score"] = value
    assert stats.score_state(compact_case(row), 1, "understanding", compact_protocol())[0] == "分数格式或范围异常"


def test_legal_na_requires_the_matching_product_explanation():
    row = compact_result()
    row["answer1_understanding_score"] = None
    row["understanding_reason"] = "产品1：正常说明；产品2：未评分（证据不足）：首屏关键内容被遮挡。"
    assert stats.score_state(compact_case(row), 1, "understanding", compact_protocol())[0] == "应评分但缺失"
    row["understanding_reason"] = "产品1：未评分（证据不足）：首屏关键内容被遮挡。；产品2：正常说明"
    assert stats.score_state(compact_case(row), 1, "understanding", compact_protocol()) == ("证据不足", None)
    row["understanding_reason"] = "产品1：未评分（无法判断）：关键上下文未提供。；产品2：正常说明"
    assert stats.score_state(compact_case(row), 1, "understanding", compact_protocol())[0] == "有说明的未评分"


def test_statistics_exclude_uncollected_diagnostics_and_accept_blocked_applicability():
    data = compact_snapshot()
    aligned = history._aligned_results(data, history._results_with_identity(data))
    tables = stats.build_compare_statistics(data, aligned)
    diagnostics = next(t for t in tables if t.title.startswith("B1．"))
    overview = next(t for t in tables if t.title.startswith("A．"))
    assert not any("核验" in str(row) for row in diagnostics.rows)
    assert not any("置信度" in str(row) or "待复核" in str(row) for row in overview.rows)
    performance = next(t for t in tables if t.title.startswith("B2．"))
    assert len(performance.rows) == 12 and performance.rows[0][3] == 4
    assert not any(row[0] == "内容准确性" for row in diagnostics.rows + performance.rows)
    case = compact_case()
    case.result.update(answer1_response_gate="fail", answer2_safety_gate="fail", understanding_applicable=None)
    diagnostics = stats._dimension_tables([case], compact_protocol(), {})[0]
    assert next(row[2] for row in diagnostics.rows if row[:2] == ["理解需求", "适用性状态缺失"]) == 0


@pytest.mark.parametrize("count", [2, 3])
def test_excel_accuracy_and_removed_columns_are_genuinely_blank(count):
    data = compact_snapshot(count)
    row = data["results"][0]
    # Even stale fields cannot accidentally leak an assessment into retired columns.
    row.update(accuracy_applicable=True, answer1_accuracy_score=5, accuracy_reason="stale",
               understanding_verification_status="verified", confidence="high", needs_human_review=False)
    workbook = load_workbook(BytesIO(history.build_xlsx(data, include_images=False)))
    sheet = workbook["逐题结果"]
    headers = [cell.value for cell in sheet[1]]
    exported = dict(zip(headers, [cell.value for cell in sheet[2]]))
    accuracy = [name for name in headers if "内容准确性" in str(name)]
    assert accuracy and all(exported[name] is None for name in accuracy)
    for name in ("理解需求核验状态", "理解需求证据", "理解需求主要问题", "理由", "证据", "置信度", "是否需要人工复核", "人工复核原因"):
        assert name in exported and exported[name] is None
    assert exported["理解需求理由"] == row["understanding_reason"]
    assert exported["产品1理解需求分"] == 4
    assert any("产品3" in str(name) for name in headers) is (count == 3)


def test_unfinished_export_uses_frozen_contract_and_old_columns_keep_old_values():
    data = compact_snapshot()
    data["results"] = []
    exported = history.export_rows(data)["逐题结果"][0]
    assert exported["产品1内容准确性分"] == exported["置信度"] == ""
    old = compact_result()
    old.update(bundle_revision="0.2.1", output_contract="compare_1022_v1", answer1_accuracy_score=4)
    exported = history._visual_compare_export_rows([old], data["protocol_manifest"])[0]
    assert list(exported) == list(history._visual_compare_export_rows([compact_result()])[0]), "retired fields must not shift Excel columns"
    assert exported["产品1内容准确性分"] == 4
    assert exported["理解需求核验状态"] == "N/A"
    legacy = resolve_compare_protocol(V1022_COMPARE_PROTOCOL_ID, "0.2.1")
    assert stats.score_state(compact_case(old), 1, "understanding", legacy) == ("核验状态缺失", None)


@pytest.mark.parametrize("revision", ["0.2.2", "0.2.3"])
def test_revision_fallback_preserves_compact_export_and_statistics(revision):
    policy = compact_protocol(revision)
    row = compact_result()
    row.update(bundle_revision=revision, answer1_accuracy_score=5, confidence="high")
    row.pop("output_contract")
    exported = history._visual_compare_export_rows([row])[0]
    assert exported["产品1内容准确性分"] == exported["置信度"] == ""
    assert exported["产品1理解需求分"] == 4
    assert stats.score_state(compact_case(row), 1, "understanding", policy) == ("有效评分", 4)
    manifest = policy.public_metadata()
    manifest.pop("output_contract")
    row.pop("bundle_revision")
    exported = history._visual_compare_export_rows([row], manifest)[0]
    assert exported["产品1内容准确性分"] == exported["置信度"] == ""


def human_fixture():
    _, snapshot, baseline, mapping = example([[4, 3]])
    policy = compact_protocol()
    snapshot.update(evaluation_profile=policy.id, protocol_manifest=policy.public_metadata())
    snapshot["results"][0].update(compact_result())
    baseline.update(human_standard_id=policy.standard_id, human_standard_version=policy.standard_version,
                    human_bundle_revision=policy.bundle_revision)
    return baseline, snapshot, mapping


def test_human_alignment_uses_compact_scores_and_frozen_revision():
    baseline, snapshot, mapping = human_fixture()
    snapshot["results"][0].update(understanding_rationale="stale", understanding_evidence=["stale"])
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["understanding"], "same_standard")
    assert all(row["comparable"] and row["model_output_contract"] == "compare_1022_v2" for row in rows)
    assert rows[0]["model_score"] == 4
    assert all(row["model_reason"] == snapshot["results"][0]["understanding_reason"] and row["model_evidence"] == [] for row in rows)
    older = deepcopy(baseline)
    older["human_bundle_revision"] = "0.2.1"
    with pytest.raises(HumanError, match="冻结修订号不同"):
        align_baseline_to_run(older, snapshot, mapping, ["case-0"], ["understanding"], "same_standard")
    rows = align_baseline_to_run(older, snapshot, mapping, ["case-0"], ["understanding"], "regression")
    assert all(row["comparable"] for row in rows)
    older.pop("human_bundle_revision")
    with pytest.raises(HumanError, match="缺少冻结修订号"):
        align_baseline_to_run(older, snapshot, mapping, ["case-0"], ["understanding"], "regression")
