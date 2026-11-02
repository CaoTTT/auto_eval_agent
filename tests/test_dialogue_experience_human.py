"""Overall experience labels retain per-product reasons and exact protocol identity."""
from io import BytesIO

from openpyxl import load_workbook
import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web.human_baselines import (
    HumanError, HumanStore, ImportMapping, atomic_json, baseline_protocol, read_json,
)
from auto_eval.web.human_baseline_import import build_human_template, inspect_workbook, parse_human_labels
from auto_eval.web.human_baseline_export import export_baseline_answers
from auto_eval.web.human_compare import ComparisonRequest, GenerateRequest, HumanComparisons, align_baseline_to_run
from auto_eval.web.human_report_export import export_report
from auto_eval.web.human_statistics import build_human_statistics
from test_human_compare import example, put_baseline

PROFILE = "dialogue_experience_compare@1.0"


def experience_example(count=2):
    task, snapshot, baseline, mapping = example([[3] * count, [5] * count])
    protocol = resolve_compare_protocol(PROFILE)
    snapshot.update(evaluation_profile=protocol.id, protocol_manifest=protocol.public_metadata())
    baseline.update(human_standard_id=protocol.standard_id, human_standard_version=protocol.standard_version,
                    human_bundle_revision=protocol.bundle_revision, human_result_schema_version=protocol.result_schema_version,
                    dimensions=["overall"], score_dimensions=["overall"],
                    dimension_names={"overall": "综合体验"}, score_range=[1, 5])
    for result in snapshot["results"]:
        result.update(standard_id=protocol.standard_id, standard_version=protocol.standard_version,
                      evaluation_profile=protocol.id, bundle_revision=protocol.bundle_revision,
                      result_schema_version=protocol.result_schema_version,
                      overall_reason="各产品共享摘要，不应冒充单产品原理由")
        for n in range(1, count + 1):
            result[f"answer{n}_overall_score"] = n + 1
            result[f"answer{n}_overall_score_status"] = "scored"
            result[f"answer{n}_overall_reason"] = f"产品{n}自己的完整理由，证据不足时暂评仍保留数字。"
            result[f"answer{n}_suggestion"] = f"产品{n}改进建议"
            result[f"answer{n}_domain"] = f"产品{n}垂域"
            result.pop(f"answer{n}_response_gate", None)
            result.pop(f"answer{n}_safety_gate", None)
    baseline["labels"] = [{**label, "dimension_id": "overall", "score": 3, "applicable": True}
                           for label in baseline["labels"]]
    return task, snapshot, baseline, mapping


def filled_template(tmp_path, fields, count=2):
    _, snapshot, _, _ = experience_example(count)
    path = tmp_path / "experience-human.xlsx"
    path.write_bytes(build_human_template(snapshot, PROFILE))
    book = load_workbook(path)
    sheet = book["人工标注"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    assert not any("Gate" in name or "是否适用" in name or "核验状态" in name for name in columns)
    assert all(sheet.cell(2, col).value is None for name, col in columns.items() if name.startswith("人工"))
    assert {name for name in columns if name.startswith("人工分_")} == {
        f"人工分_产品{n}_综合体验" for n in range(1, count + 1)
    }
    for key, value in fields.items():
        sheet.cell(2, columns[key], value)
    book.save(path)
    book.close()
    mapping = inspect_workbook(path)["suggested_mapping"]
    mapping["policy_note"] = "按本轮真实需求标注综合体验总分，保留证据状态与暂评分。"
    return path, ImportMapping(**mapping)


@pytest.mark.parametrize("count", [2, 3])
def test_overall_human_template_publish_and_answer_export_roundtrip(tmp_path, count):
    fields = {f"人工分_产品{n}_综合体验": n + 1 for n in range(1, count + 1)}
    path, mapping = filled_template(tmp_path, fields, count)
    parsed = parse_human_labels(path, mapping)
    assert not parsed["issues"]
    assert {row["dimension_id"] for row in parsed["labels"]} == {"overall"}
    store = HumanStore(tmp_path / "store")
    source = store.path("imports", "hi_experience", "source.xlsx")
    source.parent.mkdir(parents=True)
    source.write_bytes(path.read_bytes())
    atomic_json(store.path("imports", "hi_experience", "preview.json"), parsed)
    manifest = store.publish_baseline("hi_experience", parsed["preview_sha256"], "综合体验人工基准")
    assert manifest["allowed_scores_by_dimension"] == {"overall": [1, 2, 3, 4, 5]}
    baseline = store.load_baseline(manifest["baseline_id"], 1)
    path.write_bytes(export_baseline_answers(baseline))
    remapping = inspect_workbook(path)["suggested_mapping"]
    remapping["policy_note"] = "同一冻结标准重新导入"
    second = parse_human_labels(path, ImportMapping(**remapping))
    assert not second["issues"]
    assert [(row["score"], row["score_status"]) for row in second["labels"]] == [
        (row["score"], row["score_status"]) for row in parsed["labels"]
    ]


@pytest.mark.parametrize("fields", [
    {"人工分_产品1_综合体验": "5"}, {"人工分_产品1_综合体验": True},
    {"人工分_产品1_综合体验": 2.5}, {"人工分_产品1_综合体验": 0},
    {"人工分_产品1_综合体验": 6}, {"人工评分状态_产品1_综合体验": "not_applicable"},
    {"人工评分状态_产品1_综合体验": "gate_blocked"},
    {"人工评分状态_产品1_综合体验": "blocked_understanding"},
])
def test_overall_human_import_rejects_invalid_scores_and_legacy_states(tmp_path, fields):
    path, mapping = filled_template(tmp_path, fields)
    assert parse_human_labels(path, mapping)["issues"]


def test_overall_human_baseline_requires_schema_and_disallows_gates(tmp_path):
    _, _, baseline, _ = experience_example()
    baseline.pop("human_result_schema_version")
    with pytest.raises(HumanError, match="Schema"):
        baseline_protocol(baseline)
    _, mapping = filled_template(tmp_path, {})
    raw = mapping.model_dump()
    raw["gates"] = {"product1": {"response": "Z"}}
    with pytest.raises(ValueError, match="Gate"):
        ImportMapping(**raw)


@pytest.mark.parametrize("count", [2, 3])
def test_human_comparison_preserves_product_reason_and_own_prompt_fields(tmp_path, count):
    _, snapshot, baseline, mapping = experience_example(count)
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0", "case-1"], ["overall"], "same_standard")
    assert len(rows) == 2 * count and all(row["comparable"] for row in rows)
    for row in rows:
        n = row["task_product_slot"]
        assert row["model_reason"].startswith(f"产品{n}自己的完整理由")
        assert row["model_suggestion"] == f"产品{n}改进建议"
        assert row["model_domain"] == f"产品{n}垂域"
        assert row["model_reason_scope"] == "product" and row["model_gates"] == {}
    stats = build_human_statistics(rows, ["test"], [f"p{n}" for n in range(1, count + 1)], ["overall"])
    assert stats["metric_version"] == "human-statistics-dialogue-experience-1.0"
    assert all(row["kind"] == "score_status" for row in stats["states"])
    assert stats["score_policy"]["high_score_min"] == 4
    assert len(stats["pairs"]) == count * (count - 1)
    assert len(stats["ranks"]) == (1 if count == 3 else 0)
    service = HumanComparisons(HumanStore(tmp_path / "store"))
    put_baseline(service.store, baseline)
    preview = service.create_preview(ComparisonRequest(baseline_id="hb_test", version=1, tasks=[mapping]), [snapshot])
    manifest = service.prepare_report(GenerateRequest(preview_id=preview["preview_id"], config_sha256=preview["config_sha256"]))
    service.generate(manifest["report_id"])
    report = read_json(service.store.path("reports", manifest["report_id"], "report.json"))
    book = load_workbook(BytesIO(export_report(report)), read_only=True)
    headers = next(book["匹配与排除"].values)
    assert any("模型证据与理由（本产品完整原文）" in str(h) for h in headers)
    assert any("模型改进建议" in str(h) for h in headers) and any("模型垂域" in str(h) for h in headers)
    assert not any("Gate" in str(h) or "核验状态" in str(h) or "适用性" in str(h) for h in headers)
    book.close()


def test_overall_input_failure_only_excludes_affected_product():
    _, snapshot, baseline, mapping = experience_example()
    snapshot["results"][0]["answer2_input_status"] = "failed"
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["overall"], "same_standard")
    assert rows[0]["comparable"] and rows[0]["model_score"] == 2
    assert not rows[1]["comparable"] and rows[1]["model_score_status"] == "input_failed"


def test_overall_human_comparison_rejects_other_standard_family():
    _, _, baseline, mapping = experience_example()
    _, old_snapshot, _, _ = example()
    with pytest.raises(HumanError, match="家族不同"):
        align_baseline_to_run(baseline, old_snapshot, mapping, ["case-0"], ["overall"], "regression")
