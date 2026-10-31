"""Local format-recovery audit survives history and flat-chat export routes."""
from copy import deepcopy
from io import BytesIO
import json

import pytest
from openpyxl import load_workbook

from auto_eval.judges.chitchat_flat_schema import (
    CHITCHAT_FLAT_COMPARE_PROTOCOL_ID as PROFILE,
    CHITCHAT_FLAT_DIMENSIONS as DIMENSIONS,
    finalize_chitchat_flat_observation, validate_output,
)
from auto_eval.judges.compare_protocols import DEFAULT_COMPARE_PROTOCOL_ID, resolve_compare_protocol
from auto_eval.judges.product_order import remap_judge_result
from auto_eval.product_order import product_order_metadata
from auto_eval.web import history
from auto_eval.web.protocol_identity import recover_protocol_identity, require_snapshot_protocol
from auto_eval.web.tasks import Task, _task_from_snapshot


VERSION = "chitchat-flat-format-recovery-1"
AUDIT_HEADERS = ("本地格式恢复", "格式恢复版本", "格式恢复步骤")


def saved_task(reverse=False, revision=None):
    protocol = resolve_compare_protocol(PROFILE, revision)
    raw_data = {}
    for dimension in DIMENSIONS:
        raw_data[dimension + "_reason"] = '产品1：引用“原话”并说明依据；产品2：保留完整判断。'
        for product in (1, 2):
            raw_data[f"answer{product}_{dimension}_score"] = (
                5 if dimension in DIMENSIONS[:2] else product * 2)
    result = finalize_chitchat_flat_observation(validate_output(raw_data, 2), protocol)
    result.update(
        index=0, item_id="case", query="陪我聊聊",
        raw_model_output='{"style_reason":"产品1：引用"原话"；产品2：完整判断。", ...}',
        raw_key_pairs=[], original_raw_key_pairs=[],
        locally_repaired_key_pairs=list(raw_data.items()),
        repaired_model_output="", locally_repaired_json=True,
        local_format_recovery_version=VERSION,
        local_format_recovery_steps=["escape_internal_quote", "normalize_score_key:style_answer1_score->answer1_style_score"],
    )
    if reverse:
        result = remap_judge_result(result)
    result.update(product_order_metadata(reverse))
    item = {"id": "case", "query": "陪我聊聊", "product_count": 2,
            "context1": "原产品1背景", "context2": "原产品2背景",
            "answer1": "原产品1回答", "answer2": "原产品2回答"}
    return Task("recovery", "compare", [item], {"reverse_product_order": reverse},
                evaluation_profile=PROFILE, protocol_manifest=protocol.public_metadata(),
                results=[result], status="done")


@pytest.mark.parametrize("reverse", [False, True])
def test_recovery_audit_visible_in_bulk_single_csv_and_excel_without_reassigning_products(reverse):
    task = saved_task(reverse)
    result = task.results[0]
    before = deepcopy(result)
    snapshot = history.task_to_snapshot(task)
    bulk = history.export_rows(snapshot)["逐题结果"][0]
    single = history.result_export_row("compare", result, 0, task.items, PROFILE,
        task.protocol_manifest, task.results, options=task.options)
    assert bulk == single
    assert bulk["本地格式恢复"] is True
    assert bulk["格式恢复版本"] == VERSION
    assert bulk["格式恢复步骤"] == result["local_format_recovery_steps"]
    assert bulk["产品1回答"] == "原产品1回答"
    assert bulk["产品2回答"] == "原产品2回答"
    assert bulk["产品1风格恰当分"] == (4 if reverse else 2)
    assert bulk["产品2风格恰当分"] == (2 if reverse else 4)
    assert bulk["风格恰当理由（含证据与分档依据）"] == result["style_reason"]
    assert result["locally_repaired_key_pairs"][DIMENSIONS.index("style") * 3 + 1][1] == 2
    assert "raw_model_output" not in bulk and result["raw_model_output"] not in bulk.values()
    csv = history.rows_to_csv([bulk])
    assert all(header in csv for header in AUDIT_HEADERS)
    assert VERSION in csv
    workbook = load_workbook(BytesIO(history.build_xlsx(snapshot, include_images=False)), data_only=True)
    try:
        headers, values = list(workbook["逐题结果"].values)[:2]
        excel = dict(zip(headers, values))
        assert excel["本地格式恢复"] == "True"
        assert excel["格式恢复版本"] == VERSION
        assert json.loads(excel["格式恢复步骤"]) == result["local_format_recovery_steps"]
        assert excel["产品1风格恰当分"] == bulk["产品1风格恰当分"]
    finally:
        workbook.close()
    assert task.results[0] == before


@pytest.mark.parametrize("revision", ["1.1.0", "1.1.1"])
def test_history_persists_raw_output_and_recovery_audit_without_upgrading_protocol(tmp_path, monkeypatch, revision):
    task = saved_task(True, revision)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    assert history.save_task(task)
    snapshot = history.load_snapshot(task.id)
    restored = _task_from_snapshot(snapshot, task.id)
    payload = history.snapshot_payload(history.task_to_snapshot(restored))
    expected = task.results[0]
    actual = payload["results"][0]
    for field in ("raw_model_output", "raw_key_pairs", "locally_repaired_key_pairs",
                  "local_format_recovery_steps", "local_format_recovery_version",
                  "locally_repaired_json", "repaired_model_output"):
        assert json.loads(json.dumps(actual[field], ensure_ascii=False)) == json.loads(json.dumps(expected[field], ensure_ascii=False))
    assert payload["protocol_manifest"] == task.protocol_manifest
    assert recover_protocol_identity(payload)["executable"] is True
    assert require_snapshot_protocol(payload).bundle_revision == revision
    assert payload["items"][0]["answer1"] == "原产品1回答"
    assert payload["results"][0]["answer1_style_score"] == 4


def test_legacy_recovery_fields_remain_unknown_and_valid_output_needs_no_version():
    task = saved_task()
    result = task.results[0]
    for field in ("locally_repaired_json", "local_format_recovery_version", "local_format_recovery_steps"):
        result.pop(field)
    legacy = history.export_rows(history.task_to_snapshot(task))["逐题结果"][0]
    assert all(legacy[header] is None for header in AUDIT_HEADERS)
    assert "local_format_recovery_version" not in task.results[0]
    result["locally_repaired_json"] = False
    standard = history.export_rows(history.task_to_snapshot(task))["逐题结果"][0]
    assert standard["本地格式恢复"] is False
    assert standard["格式恢复版本"] is None
    assert standard["产品1风格恰当分"] == legacy["产品1风格恰当分"]


def test_unfinished_and_failed_rows_keep_audit_columns_without_inventing_scores():
    task = saved_task()
    task.items.append({"id": "pending", "query": "未完成", "product_count": 2})
    task.results[0]["error"] = "无法通过严格合同校验"
    task.results[0]["locally_repaired_json"] = False
    rows = history.export_rows(history.task_to_snapshot(task))["逐题结果"]
    assert len(rows) == 2
    assert rows[0]["格式恢复版本"] == VERSION
    assert rows[0]["产品1风格恰当分"] is None
    assert rows[1]["产品1风格恰当分"] is None
    assert all(rows[1][header] is None for header in AUDIT_HEADERS)


@pytest.mark.parametrize("profile", [DEFAULT_COMPARE_PROTOCOL_ID, "knowledge_qa_compare@1.0", "timeliness_compare@1.0"])
def test_other_protocol_exports_do_not_gain_flat_recovery_columns(profile):
    protocol = resolve_compare_protocol(profile)
    snapshot = {"mode": "compare", "evaluation_profile": profile,
                "protocol_manifest": protocol.public_metadata(),
                "items": [{"id": "one", "query": "问题", "product_count": 2}], "results": []}
    row = history.export_rows(snapshot)["逐题结果"][0]
    assert all(header not in row for header in AUDIT_HEADERS)
