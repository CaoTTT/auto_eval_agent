"""Cross-product conflict classification survives history and all result exports."""
from copy import deepcopy
from io import BytesIO

from openpyxl import load_workbook
import pytest

from auto_eval.web.compare_statistics import _cases, build_compare_statistics
from auto_eval.web.history import build_xlsx, export_rows, result_export_row, snapshot_payload
from auto_eval.web.tasks import _task_from_snapshot
from auto_eval.web.timeliness_statistics import conflict_metrics
from test_timeliness_statistics import dataset, summary


@pytest.mark.parametrize("count", (2, 3))
def test_conflict_classification_survives_history_and_export_without_changing_scores(count):
    snapshot, rows, _ = dataset(((3,) * count,))
    rows[0].update(conflict_reason="产品1和产品2对同日闭园时间分别明确说18点、20点，口径一致且互斥。",
                   has_conflict="yes")
    original = deepcopy(snapshot)
    restored = _task_from_snapshot(snapshot, "time-test")
    assert restored.protocol_manifest["result_schema_version"] == "timeliness-result-3"
    assert snapshot_payload(snapshot)["results"][0]["has_conflict"] == "yes"
    export = export_rows(snapshot)["逐题结果"][0]
    assert export["内容冲突理由"] == rows[0]["conflict_reason"]
    assert export["内容冲突"] == "有冲突"
    assert list(export).index("内容冲突理由") < list(export).index("内容冲突")
    single = result_export_row("compare", rows[0], 0, snapshot["items"],
        evaluation_profile=snapshot["evaluation_profile"], protocol_manifest=snapshot["protocol_manifest"])
    assert single["内容冲突"] == "有冲突"
    wb = load_workbook(BytesIO(build_xlsx(snapshot)), read_only=True)
    try:
        headers, values = list(wb["逐题结果"].values)[:2]
        assert dict(zip(headers, values))["内容冲突"] == "有冲突"
        assert "内容冲突理由" in headers
    finally:
        wb.close()
    live = summary(snapshot)
    assert live["conflict_yes"] == live["conflict_known"] == 1
    assert live["conflict_rate"] == 1
    assert live["authority_answer1_avg"] == 3
    assert snapshot == original


@pytest.mark.parametrize("revision", ("1.0.0", "1.0.1"))
def test_old_history_is_explicitly_uncollected_and_never_counted_as_no(revision):
    snapshot, rows, _ = dataset(revision=revision)
    restored = _task_from_snapshot(snapshot, "time-test")
    assert restored.protocol_manifest["bundle_revision"] == revision
    assert "has_conflict" not in rows[0]
    live = summary(snapshot)
    assert live["conflict_not_collected"] == 1
    assert live["conflict_no"] == live["conflict_known"] == 0
    assert live["conflict_rate"] is None
    export = export_rows(snapshot)["逐题结果"][0]
    assert export["内容冲突采集状态"] == "未采集（旧协议）"
    assert "内容冲突" not in export


def test_conflict_denominator_separates_unclear_invalid_failed_and_unfinished():
    snapshot, rows, protocol = dataset(((3, 3, 3),) * 7)
    for row, verdict in zip(rows, ("yes", "no", "no", "unclear")):
        row.update(has_conflict=verdict, conflict_reason="同口径证据对比说明。")
    rows[4].pop("conflict_reason")
    snapshot["results"] = rows[:5] + [dict(index=5, error="timeout")]
    live = summary(snapshot)
    assert [live[f"conflict_{key}"] for key in
            ("yes", "no", "unclear", "invalid_state", "technical_failure", "unfinished")] == [1, 2, 1, 1, 1, 1]
    assert live["conflict_known"] == 3
    assert live["conflict_rate"] == pytest.approx(1 / 3)
    aligned = snapshot["results"] + [dict(index=6, **{"评估状态": "待评估"})]
    table = next(t for t in build_compare_statistics(snapshot, aligned) if t.title == "内容冲突（逐题跨产品）")
    assert table.rows[0][1:4] == [1, 3, pytest.approx(1 / 3)]
    assert "每题计一次" in table.rows[0][-1]
    assert conflict_metrics(_cases(snapshot, aligned), protocol)["conflict_total"] == 7


@pytest.mark.parametrize("count,verdict,failed_product", ((2, "no", 2), (3, "yes", 3)))
def test_collection_failure_keeps_raw_conflict_but_excludes_clear_denominator(count, verdict, failed_product):
    snapshot, rows, protocol = dataset(((3,) * count,))
    reason = "产品1与产品2的可见核心结论对比；产品3若存在则保留其独立采集事实。"
    rows[0].update(has_conflict=verdict, conflict_reason=reason,
                   **{f"answer{failed_product}_input_status": "failed"})
    live = summary(snapshot)
    assert live["done"] == 1 and live["failed"] == 0
    assert live["conflict_input_failed"] == 1
    assert live["conflict_yes"] == live["conflict_no"] == live["conflict_known"] == 0
    assert live["conflict_rate"] is None
    saved = snapshot_payload(snapshot)["results"][0]
    assert saved["has_conflict"] == verdict and saved["conflict_reason"] == reason
    export = export_rows(snapshot)["逐题结果"][0]
    assert export["内容冲突采集状态"] == "采集失败，待核验"
    assert export["内容冲突"] == f"采集失败，待核验（模型：{'有冲突' if verdict == 'yes' else '无冲突'}）"
    assert export["模型内容冲突原始值"] == verdict
    assert export["内容冲突理由"] == reason
    table = next(t for t in build_compare_statistics(snapshot, rows) if t.title == "内容冲突（逐题跨产品）")
    assert next(row for row in table.rows if row[0] == "采集失败，待核验")[1] == 1
    assert table.rows[0][2:4] == [0, None]
    assert conflict_metrics(_cases(snapshot, rows), protocol)["conflict_input_failed"] == 1


def test_inactive_product_failure_does_not_block_two_product_conflict():
    snapshot, rows, _ = dataset()
    rows[0].update(has_conflict="yes", conflict_reason="两产品同口径明确互斥。",
                   answer3_input_status="failed")
    assert summary(snapshot)["conflict_yes"] == 1
    assert summary(snapshot)["conflict_input_failed"] == 0
