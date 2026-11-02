"""Overall GSB reports retain categorical decisions and honest denominators."""
from copy import deepcopy
from io import BytesIO
from itertools import combinations

from openpyxl import load_workbook
import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web.compare_statistics import build_compare_statistics
from auto_eval.web.history import build_xlsx, export_rows, result_export_row, snapshot_payload
from auto_eval.web.protocol_identity import recover_protocol_identity
from auto_eval.web.runner import _summarize
from auto_eval.web.tasks import Task


PROFILE = "chitchat_gsb_compare@1.0"
SCHEMA = "chitchat-gsb-result-1"


def pair(gsb="G", *, status=None, serious_issue="none", **changes):
    value = {
        "status": status or ("evaluated" if gsb is not None else "insufficient_evidence"),
        "gsb": gsb,
        "reason": "产品1具体承接本轮倾诉，产品2主要给未请求的方案；前者更贴合当前诉求。",
        "serious_issue": serious_issue,
    }
    value.update(changes)
    return value


def dataset(decisions=None, *, count=2):
    protocol = resolve_compare_protocol(PROFILE)
    snapshot = {
        "task_id": "gsb-report-test", "mode": "compare",
        "evaluation_profile": protocol.id, "protocol_manifest": protocol.public_metadata(),
        "options": {}, "items": [], "results": [],
    }
    for index, decisions_by_pair in enumerate(decisions or [{"1_2": pair()}]):
        item = {"id": f"gsb-{index}", "query": "我只想吐槽，别给建议。", "product_count": count}
        for product in range(1, count + 1):
            item[f"context{product}"] = f"产品{product}已知背景"
            item[f"answer{product}"] = f"产品{product}回答正文"
        row = {
            "index": index, "item_id": item["id"], "query": item["query"],
            "product_count": count, "evaluation_profile": protocol.id,
            "standard_id": protocol.standard_id, "standard_version": protocol.standard_version,
            "bundle_revision": protocol.bundle_revision, "result_schema_version": SCHEMA,
            "gsb_pairs": {}, "评估状态": "已完成",
        }
        for a, b in combinations(range(1, count + 1), 2):
            key = f"{a}_{b}"
            value = deepcopy(decisions_by_pair.get(key, pair("S")))
            row["gsb_pairs"][key] = {"product_a": a, "product_b": b, **value}
        snapshot["items"].append(item)
        snapshot["results"].append(row)
    return snapshot


def summarize(snapshot):
    return _summarize(Task(
        snapshot["task_id"], "compare", snapshot["items"], snapshot["options"],
        evaluation_profile=PROFILE, protocol_manifest=snapshot["protocol_manifest"],
        results=snapshot["results"],
    ))


def test_only_valid_gsb_enters_denominator_and_other_states_stay_distinct():
    snapshot = dataset([
        {"1_2": pair("G")}, {"1_2": pair("G")}, {"1_2": pair("S")},
        {"1_2": pair("B")}, {"1_2": pair(None)},
        {"1_2": pair(None, status="needs_review")},
        {"1_2": pair("G")}, {"1_2": pair("G")},
        {"1_2": pair("g")},
    ])
    snapshot["results"][6]["error"] = "mock judge transport failed"
    snapshot["results"][6]["评估状态"] = "评估失败"
    snapshot["results"].pop(7)
    summary = summarize(snapshot)
    metrics = summary["gsb_pairs"]["1_2"]
    assert (metrics["G"], metrics["S"], metrics["B"]) == (2, 1, 1)
    assert metrics["valid_count"] == metrics["evaluated"] == 4
    assert metrics["total"] == metrics["eligible"] == 9
    assert metrics["net_win_rate"] == .25
    assert metrics["insufficient_evidence"] == 1
    assert metrics["needs_review"] == 1
    assert metrics["failed"] == 1
    assert metrics["pending"] == 1
    assert metrics["invalid_state"] == 1
    assert (summary["done"], summary["failed"], summary["unfinished"]) == (7, 1, 1)


def test_zero_valid_decisions_have_no_numeric_net_win_rate():
    snapshot = dataset([{"1_2": pair(None)}, {"1_2": pair(None, status="needs_review")}])
    metrics = summarize(snapshot)["gsb_pairs"]["1_2"]
    assert metrics["valid_count"] == 0
    assert metrics["net_win_rate"] is None
    assert metrics["S"] == 0


@pytest.mark.parametrize("issue,decision", [("A", "G"), ("A", "S"), ("B", "B"), ("B", "S")])
def test_historical_serious_issue_and_gsb_direction_conflict_is_not_a_valid_vote(issue, decision):
    snapshot = dataset([{"1_2": pair(decision, serious_issue=issue)}])
    metrics = summarize(snapshot)["gsb_pairs"]["1_2"]
    assert metrics["valid_count"] == 0
    assert metrics["invalid_state"] == 1
    assert metrics["net_win_rate"] is None
    row = export_rows(snapshot)["逐题结果"][0]
    assert row["产品1对产品2 GSB"] is None
    assert row["产品1对产品2 判定状态"] == "状态异常"


def test_retries_replace_the_same_case_instead_of_adding_votes():
    snapshot = dataset()
    retry = deepcopy(snapshot["results"][0])
    retry["gsb_pairs"]["1_2"]["gsb"] = "B"
    snapshot["results"].append(retry)
    metrics = summarize(snapshot)["gsb_pairs"]["1_2"]
    assert (metrics["valid_count"], metrics["G"], metrics["B"]) == (1, 0, 1)
    assert metrics["net_win_rate"] == -1
    exported = export_rows(snapshot)["逐题结果"]
    assert len(exported) == 1
    assert exported[0]["产品1对产品2 GSB"] == "B"


def test_three_products_keep_independent_pairs_without_scores_or_forced_ranks():
    snapshot = dataset([{"1_2": pair("G"), "1_3": pair("B"), "2_3": pair("G")}], count=3)
    summary = summarize(snapshot)
    assert set(summary["gsb_pairs"]) == {"1_2", "1_3", "2_3"}
    assert [summary["gsb_pairs"][key]["net_win_rate"] for key in ("1_2", "1_3", "2_3")] == [1, -1, 1]
    assert not any(key.endswith(("_avg", "_score", "_rank_groups")) for key in summary)
    rows = export_rows(snapshot)["逐题结果"]
    assert rows[0]["产品2对产品3 GSB"] == "G"
    assert not any("Gate" in key or "综合体验分" in key or "理解需求分" in key for key in rows[0])
    tables = build_compare_statistics(snapshot, snapshot["results"])
    assert all(len(row) == len(table.headers) for table in tables for row in table.rows)
    assert not any("排序" in table.title or "均分" in table.title for table in tables)


def test_trusted_capture_failure_excludes_only_pairs_touching_that_product():
    snapshot = dataset([{"1_2": pair("G"), "1_3": pair("B"), "2_3": pair("S")}], count=3)
    row = snapshot["results"][0]
    row["trusted_collection_metadata"] = {"answer1": {"input_status": "failed", "source": "trusted_program"}}
    row["answer1_input_status"] = "failed"
    pairs = summarize(snapshot)["gsb_pairs"]
    assert pairs["1_2"]["valid_count"] == pairs["1_3"]["valid_count"] == 0
    assert pairs["2_3"]["valid_count"] == 1
    assert pairs["2_3"]["S"] == 1
    exported = export_rows(snapshot)["逐题结果"][0]
    assert exported["产品1对产品2 GSB"] is None
    assert exported["产品2对产品3 GSB"] == "S"


@pytest.mark.parametrize("field,value", [
    ("gsb_pairs", []), ("gsb_pairs", {"1_2": "not-an-object"}),
    ("trusted_collection_metadata", ["invalid"]),
    ("trusted_collection_metadata", {"answer1": ["invalid"]}),
])
def test_malformed_saved_pair_or_collection_objects_fail_closed(field, value):
    snapshot = dataset()
    snapshot["results"][0][field] = value
    metrics = summarize(snapshot)["gsb_pairs"]["1_2"]
    assert metrics["valid_count"] == 0
    assert metrics["invalid_state"] == 1
    exported = export_rows(snapshot)["逐题结果"][0]
    assert exported["产品1对产品2 GSB"] is None
    assert exported["产品1对产品2 判定状态"] == "状态异常"


@pytest.mark.parametrize("change", ["missing_revision", "missing_schema", "wrong_schema", "unknown_revision"])
def test_incomplete_identity_keeps_raw_but_cannot_reinterpret_gsb(change):
    snapshot = dataset()
    if change.startswith("missing"):
        field = "bundle_revision" if change == "missing_revision" else "result_schema_version"
        snapshot["protocol_manifest"].pop(field)
        snapshot["results"][0].pop(field)
    else:
        field, value = ("result_schema_version", "chitchat-gsb-result-99") if change == "wrong_schema" else ("bundle_revision", "99.0.0")
        snapshot["protocol_manifest"][field] = value
        snapshot["results"][0][field] = value
    snapshot["summary"] = {"total": 1, "done": 1, "gsb_pairs": {"1_2": {"G": 1, "net_win_rate": 1}}}
    before = deepcopy(snapshot)
    recovered = recover_protocol_identity(snapshot)
    assert recovered["is_chitchat_gsb"] and not recovered["executable"]
    payload = snapshot_payload(snapshot)
    assert not payload["protocol_executable"]
    assert payload["results"] == before["results"]
    assert payload["raw_summary"] == before["summary"]
    assert "gsb_pairs" not in payload["summary"]
    assert export_rows(snapshot)["逐题结果"][0]["产品1对产品2 GSB"] is None
    assert snapshot == before


def test_full_and_single_exports_keep_sources_reasons_and_metadata(monkeypatch):
    monkeypatch.delenv("EXPORT_PUBLIC_BASE_URL", raising=False)
    snapshot = dataset(count=3)
    snapshot["export_base_url"] = "https://eval.example"
    item = snapshot["items"][0]
    item.update(screenshot1="/tmp/gsb-original.png", screenshot2="/tmp/gsb-original-2.png",
                video3="https://example.test/third.mp4")
    row = snapshot["results"][0]
    row["gsb_pairs"]["1_2"]["serious_issue"] = "B"
    exported = export_rows(snapshot)["逐题结果"][0]
    # Single-result exports lack the request origin, so compare the shared source fields.
    single = result_export_row("compare", row, 0, snapshot["items"], PROFILE,
                               snapshot["protocol_manifest"], snapshot["results"])
    for key in ("产品1对产品2 GSB", "产品1对产品2 判定状态", "产品1对产品2 理由",
                "产品1对产品2 严重问题", "产品1背景", "产品2回答", "结果Schema版本"):
        assert single[key] == exported[key]
    assert exported["产品1对产品2 GSB"] == "G"
    assert exported["产品1对产品2 判定状态"] == "已评"
    assert exported["产品1对产品2 严重问题"] == "B"
    assert exported["产品1背景"] == item["context1"]
    assert exported["产品2回答"] == item["answer2"]
    assert exported["产品1长截图查看链接"] == "https://eval.example/api/eval/gsb-report-test/items/0/screenshots/1"


def test_xlsx_preserves_numeric_gsb_and_percentage_format():
    snapshot = dataset([{"1_2": pair("G")}, {"1_2": pair("G")},
                        {"1_2": pair("S")}, {"1_2": pair("B")}, {"1_2": pair(None)}])
    book = load_workbook(BytesIO(build_xlsx(snapshot, include_images=False)), data_only=True)
    try:
        sheet = book["对比评估统计"]
        matches = []
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value == "净胜率":
                    candidate = sheet.cell(cell.row + 1, cell.column)
                    if candidate.value == .25:
                        matches.append(candidate)
        assert matches, "GSB统计表应保存有效配对样本上的数值净胜率"
        assert any("%" in cell.number_format for cell in matches)
        headers = [cell.value for cell in book["逐题结果"][1]]
        assert "产品1对产品2 GSB" in headers
        assert not any(value and ("Gate" in str(value) or "综合体验分" in str(value)) for value in headers)
    finally:
        book.close()


def test_multiturn_reports_are_explicitly_turn_level_not_invented_session_votes():
    snapshot = dataset([{"1_2": pair("G")}, {"1_2": pair("B")}, {"1_2": pair("G")}])
    for index, item in enumerate(snapshot["items"][:2], 1):
        item.update(session_id="one", turn_index=index, screenshot_scope="current_turn")
    summary = summarize(snapshot)
    assert summary["statistic_unit"] == "turn"
    assert summary["gsb_pairs"]["1_2"]["valid_count"] == 3
    assert summary["gsb_pairs"]["1_2"]["net_win_rate"] == pytest.approx(1 / 3)
    assert summary.get("conversation_statistics")
    text = " ".join(str(table) for table in summary["conversation_statistics"])
    assert "one" in text
    assert not any(key in summary for key in ("session_gsb", "session_winner", "conversation_gsb"))
