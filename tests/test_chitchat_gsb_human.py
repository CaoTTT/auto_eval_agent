"""Categorical GSB labels remain separate from numeric human score contracts."""
from copy import deepcopy
from io import BytesIO
from itertools import combinations

from openpyxl import load_workbook
import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web.human_baseline_import import build_human_template, inspect_workbook, parse_human_labels
from auto_eval.web.human_baseline_export import export_baseline_answers
from auto_eval.web.human_baselines import HumanError, HumanStore, ImportMapping, atomic_json, baseline_protocol, read_json
from auto_eval.web.human_compare import (ComparisonRequest, GenerateRequest, HumanComparisons, align_baseline_to_run)
from auto_eval.web.human_statistics import build_human_statistics
from auto_eval.web.human_report_export import export_report
from test_human_compare import example, put_baseline

PROFILE = "chitchat_gsb_compare@1.0"


def gsb_example(count=2):
    _, snapshot, baseline, mapping = example([[3] * count, [4] * count])
    protocol = resolve_compare_protocol(PROFILE)
    snapshot.update(evaluation_profile=protocol.id, protocol_manifest=protocol.public_metadata())
    baseline.update(human_standard_id=protocol.standard_id, human_standard_version=protocol.standard_version,
                    human_bundle_revision=protocol.bundle_revision, human_result_schema_version=protocol.result_schema_version,
                    dimensions=["overall"], score_dimensions=[], dimension_names={"overall": "整体GSB"}, score_range=None)
    baseline["labels"] = []
    for case, result in zip(baseline["cases"], snapshot["results"]):
        result.update(evaluation_profile=protocol.id, standard_id=protocol.standard_id, standard_version=protocol.standard_version,
                      bundle_revision=protocol.bundle_revision, result_schema_version=protocol.result_schema_version,
                      product_count=count, gsb_pairs={})
        for a, b in combinations(range(1, count + 1), 2):
            result["gsb_pairs"][f"{a}_{b}"] = dict(product_a=a, product_b=b, status="evaluated", gsb="G",
                                                  reason=f"产品{a}回应核心需求，产品{b}有实质缺口。", serious_issue="none")
            baseline["labels"].append(dict(case_key=case["case_key"], product_a=f"p{a}", product_b=f"p{b}",
                dimension_id="overall", status="evaluated", gsb="G", reason="人工有可见证据的整体比较理由。",
                serious_issue="none", resolution="accepted", annotation_stage="base", source={}))
    return snapshot, baseline, mapping


def filled_template(tmp_path, fields, count=2):
    snapshot, _, _ = gsb_example(count)
    path = tmp_path / "gsb-human.xlsx"
    path.write_bytes(build_human_template(snapshot, PROFILE))
    book = load_workbook(path)
    sheet = book["人工标注"]
    columns = {cell.value: cell.column for cell in sheet[1]}
    assert not any("人工分_" in name or "Gate" in name for name in columns)
    for key, value in fields.items():
        sheet.cell(2, columns[key], value)
    book.save(path)
    book.close()
    mapping = inspect_workbook(path)["suggested_mapping"]
    mapping["policy_note"] = "按同题同产品对整体GSB标注，证据不足不计平局"
    return path, ImportMapping(**mapping)


@pytest.mark.parametrize("count", [2, 3])
def test_gsb_template_publish_and_roundtrip(tmp_path, count):
    fields = {}
    for a, b in combinations(range(1, count + 1), 2):
        fields[f"人工GSB_产品{a}对产品{b}"] = "G"
        fields[f"人工理由_产品{a}对产品{b}"] = "A已回应核心诉求，B只有泛泛表态，实际帮助不同。"
    path, mapping = filled_template(tmp_path, fields, count)
    parsed = parse_human_labels(path, mapping)
    assert not parsed["issues"]
    assert not mapping.labels and len(mapping.pair_labels) == count * (count - 1) // 2
    assert all("score" not in row for row in parsed["labels"])
    store = HumanStore(tmp_path / "store")
    source = store.path("imports", "hi_gsb", "source.xlsx")
    source.parent.mkdir(parents=True)
    source.write_bytes(path.read_bytes())
    atomic_json(store.path("imports", "hi_gsb", "preview.json"), parsed)
    manifest = store.publish_baseline("hi_gsb", parsed["preview_sha256"], "GSB人工基准")
    assert manifest["score_range"] is None and manifest["score_dimensions"] == []
    baseline = store.load_baseline(manifest["baseline_id"], 1)
    path.write_bytes(export_baseline_answers(baseline))
    mapping = inspect_workbook(path)["suggested_mapping"]
    mapping["policy_note"] = "同一冻结GSB标准再次导入"
    second = parse_human_labels(path, ImportMapping(**mapping))
    assert not second["issues"]
    assert [(x["status"], x["gsb"]) for x in second["labels"]] == [(x["status"], x["gsb"]) for x in parsed["labels"]]


@pytest.mark.parametrize("value", [5, 0, True, "5", "g", "NA", "tie"])
def test_gsb_does_not_accept_absolute_scores_or_implicit_ties(tmp_path, value):
    path, mapping = filled_template(tmp_path, {"人工GSB_产品1对产品2": value,
        "人工理由_产品1对产品2": "不能从原评分换算。"})
    assert parse_human_labels(path, mapping)["issues"]


def test_missing_evidence_never_becomes_s_and_review_overrides(tmp_path):
    path, mapping = filled_template(tmp_path, {"人工GSB_产品1对产品2": "G", "人工复核GSB_产品1对产品2": "B",
        "人工理由_产品1对产品2": "复核后确认B实际更符合诉求。"})
    parsed = parse_human_labels(path, mapping)
    assert not parsed["issues"] and parsed["labels"][0]["gsb"] == "B"
    path, mapping = filled_template(tmp_path, {"人工状态_产品1对产品2": "insufficient_evidence", "人工理由_产品1对产品2": "关键上文缺失。"})
    row = parse_human_labels(path, mapping)["labels"][0]
    assert row["gsb"] is None and row["status"] == "insufficient_evidence"
    path, mapping = filled_template(tmp_path, {"人工状态_产品1对产品2": "insufficient_evidence", "人工GSB_产品1对产品2": "S", "人工理由_产品1对产品2": "缺证不能判S。"})
    assert parse_human_labels(path, mapping)["issues"]


def test_gsb_requires_frozen_schema_and_rejects_numeric_mapping(tmp_path):
    _, baseline, _ = gsb_example()
    baseline.pop("human_result_schema_version")
    with pytest.raises(HumanError, match="Schema"):
        baseline_protocol(baseline)
    path, mapping = filled_template(tmp_path, {})
    raw = mapping.model_dump()
    raw["human_bundle_revision"] = ""
    with pytest.raises(ValueError, match="冻结修订号"):
        ImportMapping(**raw)
    raw = mapping.model_dump()
    raw["labels"] = [dict(product_id="product1", dimension_id="overall", score="A")]
    with pytest.raises(ValueError, match="数字分"):
        ImportMapping(**raw)
    raw = mapping.model_dump()
    raw["human_standard_id"] = "dialogue_experience_compare"
    raw["human_result_schema_version"] = "dialogue-experience-result-1"
    with pytest.raises(ValueError, match="数字评分标准"):
        ImportMapping(**raw)


@pytest.mark.parametrize("count", [2, 3])
def test_gsb_comparison_and_report_pipeline(tmp_path, count):
    snapshot, baseline, mapping = gsb_example(count)
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0", "case-1"], ["overall"], "same_standard")
    assert len(rows) == count * (count - 1)
    assert all(row["comparable"] and row["agreement"] and row["human_score"] is None for row in rows)
    rows[0]["model_gsb"] = "B"
    from auto_eval.web.human_compare import finalize_row
    finalize_row(rows[0])
    metrics = build_human_statistics(rows, ["test"], [p["product_id"] for p in baseline["products"]], ["overall"])
    assert metrics["metric_version"] == "human-statistics-chitchat-gsb-1.0"
    assert not metrics["scores"] and not metrics["ranks"]
    assert metrics["gsb_agreement"][0]["reversal"] == .5
    service = HumanComparisons(HumanStore(tmp_path / "store"))
    put_baseline(service.store, baseline)
    preview = service.create_preview(ComparisonRequest(baseline_id="hb_test", version=1, tasks=[mapping]), [snapshot])
    assert preview["comparison_kind"] == "gsb" and preview["comparable_n"] == len(rows)
    manifest = service.prepare_report(GenerateRequest(preview_id=preview["preview_id"], config_sha256=preview["config_sha256"]))
    service.generate(manifest["report_id"])
    report = read_json(service.store.path("reports", manifest["report_id"], "report.json"))
    book = load_workbook(BytesIO(export_report(report)), read_only=True)
    assert "GSB一致性" in book and "逐题GSB对比" in book
    assert not any("均分" in str(v) or "MAE" in str(v) for row in book["GSB一致性"].values for v in row)
    book.close()


def test_gsb_excludes_failed_pair_but_keeps_unrelated_pair():
    snapshot, baseline, mapping = gsb_example(3)
    snapshot["results"][0]["trusted_collection_metadata"] = {"answer2": {"input_status": "failed"}}
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["overall"], "same_standard")
    assert [r["comparable"] for r in rows] == [False, True, False]
    assert rows[0]["model_gsb"] is None


def test_gsb_swapped_slot_direction_and_both_answer_identity():
    snapshot, baseline, mapping = gsb_example()
    for item, result in zip(snapshot["items"], snapshot["results"]):
        item["answer1"], item["answer2"] = item["answer2"], item["answer1"]
        result["answer1"], result["answer2"] = result["answer2"], result["answer1"]
        result["gsb_pairs"]["1_2"]["gsb"] = "B"
    mapping = mapping.model_copy(update={"product_map": {"answer1": "p2", "answer2": "p1"}})
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["overall"], "same_standard")
    assert rows[0]["model_gsb"] == "G" and rows[0]["comparable"]
    snapshot["items"][0]["answer1"] = "changed B response"
    assert not align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["overall"], "same_standard")[0]["comparable"]


def test_gsb_unknown_or_other_protocol_and_multiturn_history_excluded():
    snapshot, baseline, mapping = gsb_example()
    _, old, _, _ = example()
    with pytest.raises(HumanError, match="相同完整标准"):
        align_baseline_to_run(baseline, old, mapping, ["case-0"], ["overall"], "regression")
    snapshot["items"][0].update(session_id="session-a", turn_index=2, history_prefix_sha256="new")
    row = align_baseline_to_run(baseline, snapshot, mapping, ["case-0"], ["overall"], "same_standard")[0]
    assert row["match_status"] == "history_unverified" and not row["comparable"]


def test_gsb_rejects_contradictory_serious_issue(tmp_path):
    path, mapping = filled_template(tmp_path, {"人工GSB_产品1对产品2": "G", "人工严重问题_产品1对产品2": "A", "人工理由_产品1对产品2": "存在严重问题。"})
    assert parse_human_labels(path, mapping)["issues"]


@pytest.mark.asyncio
async def test_gsb_http_rows_filter_categories_without_numeric_scores(tmp_path):
    from fastapi import FastAPI
    import httpx
    from auto_eval.web.human_routes import install_human_routes
    from auto_eval.web.human_compare import finalize_row
    snapshot, baseline, mapping = gsb_example()
    rows = align_baseline_to_run(baseline, snapshot, mapping, ["case-0", "case-1"], ["overall"], "same_standard")
    rows[1]["model_gsb"] = "B"
    finalize_row(rows[1])
    service = HumanComparisons(HumanStore(tmp_path / "store"))
    atomic_json(service.store.path("reports", "hr_gsb"), {"status": "ready"})
    atomic_json(service.store.path("reports", "hr_gsb", "report.json"), {"baseline": baseline, "rows": rows})
    app = FastAPI()
    async def peek(_):
        return None
    install_human_routes(app, lambda: service, peek)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for direction, expected in (("agreement", 1), ("disagreement", 1), ("reversal", 1), ("higher", 0)):
            response = await client.get(f"/api/human-comparisons/hr_gsb/rows?direction={direction}")
            assert response.status_code == 200 and response.json()["total"] == expected
        response = await client.get("/api/human-comparisons/hr_gsb/rows?min_difference=1")
        assert response.json()["total"] == 0
