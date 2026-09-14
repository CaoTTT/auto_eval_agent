"""Known-answer checks for sampling, reciprocal ratios and exported OOXML."""
import copy
from io import BytesIO
import math
from xml.etree import ElementTree as ET
import zipfile

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.web import compare_statistics as stats, history


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def make_snapshot(scores, standard="0.2-simplified", *, count=None):
    count = count or len(scores[0])
    protocol = resolve_compare_protocol(f"qa_competitor_compare@{standard}")
    items, results = [], []
    for index, values in enumerate(scores):
        items.append({"id": f"case-{index}", "query": f"问题{index}", "product_count": count,
                      "evidence_mode": "video_frames", "category": "通用"})
        row = {"index": index, "product_count": count, "standard_version": standard,
               "bundle_revision": protocol.bundle_revision, "confidence": "high", "has_conflict": "no"}
        for product in range(1, count + 1):
            row.update({f"answer{product}_input_status": "complete", f"answer{product}_response_gate": "pass",
                        f"answer{product}_safety_gate": "pass"})
        for dimension in stats.DIMENSIONS:
            row.update({f"{dimension}_applicable": True, f"{dimension}_verification_status": "not_required"})
            for product, score in enumerate(values, 1):
                row[f"answer{product}_{dimension}_score"] = score
        results.append(row)
    return {"mode": "compare", "status": "done", "task_id": "test", "dataset_name": "样本.jsonl",
            "evaluation_profile": protocol.id, "protocol_manifest": protocol.public_metadata(),
            "items": items, "results": results}


def report(snapshot):
    return stats.build_compare_statistics(snapshot, history._aligned_results(snapshot, history._results_with_identity(snapshot)))


def table(tables, prefix):
    return next(t for t in tables if t.title.startswith(prefix))


def record(tables, prefix, **match):
    selected = table(tables, prefix)
    return next(row for values in selected.rows if all((row := dict(zip(selected.headers, values))).get(k) == v for k, v in match.items()))


@pytest.mark.parametrize("standard", ["0.2-simplified", "0.3"])
@pytest.mark.parametrize("count", [2, 3])
def test_all_product_directions_and_score_ranges(standard, count):
    scores = ([4, 5, 2], [5, 3, 4]) if standard == "0.2-simplified" else ([3, 2, 1], [1, 2, 3])
    data = make_snapshot([s[:count] for s in scores], standard)
    tables = report(data)
    directions = {row[1] for row in table(tables, "C1．").rows}
    assert directions == {f"产品{a} / 产品{b}" for a in range(1, count + 1) for b in range(1, count + 1) if a != b}
    assert len(table(tables, "C1．").rows) == 6 * count * (count - 1)
    assert len(table(tables, "C2．").rows) == 6 * count * (count - 1)
    bins = {row[2] for row in table(tables, "B3．").rows}
    assert bins == (set(range(1, 6)) if standard == "0.2-simplified" else set(range(4)))
    assert any(t.title.startswith("D．") for t in tables) == (count == 3)
    for t in tables:
        assert all(len(row) == len(t.headers) for row in t.rows)
        assert not t.formats or len(t.formats) == len(t.headers)
    assert not any(row[0] == "内容准确性" for t in tables if t.title.startswith(("B2．", "B3．", "C1．", "C2．", "D．")) for row in t.rows)


def test_known_pair_means_ratios_gsb_and_sample_alignment():
    data = make_snapshot([[4, 5], [5, 3], [3, 3], [None, 1]])
    # Deliberately bogus cached summary, winner and ranking must not be used.
    data["summary"] = {"understanding_answer1_avg": 999, "failed": 99}
    data["results"][0]["understanding_rank_groups"] = [["product1"]]
    before = copy.deepcopy(data)
    tables = report(data)
    ab = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    ba = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品2 / 产品1"})
    assert ab["配对有效题数"] == 3 and ab["适用题数"] == 4
    assert ab["可比覆盖率"] == .75
    assert ab["A均分"] == 4 and ab["B均分"] == pytest.approx(11 / 3)
    assert ab["分位值 A/B"] == pytest.approx(12 / 11)
    assert ba["分位值 A/B"] == pytest.approx(11 / 12)
    assert ab["均分差 A-B"] == pytest.approx(1 / 3)
    assert ba["均分差 A-B"] == pytest.approx(-1 / 3)
    assert ab["达标率差"] == pytest.approx(1 / 3)
    gsb = record(tables, "C2．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert gsb["GSB数量"] == "1/1/1" and gsb["净胜率"] == 0
    distribution = [row for row in table(tables, "C3．").rows if row[:2] == ["理解需求", "产品1 / 产品2"]]
    assert sum(row[3] for row in distribution) == 3
    assert ab["分差95%下限"] == -ba["分差95%上限"]
    assert ab["分差95%上限"] == -ba["分差95%下限"]
    assert data == before


def test_three_way_missing_c_does_not_discard_ab_and_ties_are_distinct():
    data = make_snapshot([[5, 4, 3], [5, 5, 3], [4, 4, 4], [3, 4, None]])
    data["results"][-1]["answer3_input_status"] = "failed"
    tables = report(data)
    assert record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == 4
    assert record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品3"})["配对有效题数"] == 3
    common = record(tables, "C1．双向分位值与配对分差（三方", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert common["配对有效题数"] == 3
    first = record(tables, "D．", 维度="理解需求", 产品="产品1")
    assert first["独占第一数"] == first["两方并列第一数"] == first["三方同分数"] == 1
    assert first["三方有效题数"] == 3
    third = record(tables, "D．", 维度="理解需求", 产品="产品3")
    assert third["独占末位数"] == 2


@pytest.mark.parametrize("gate,status,expected_v02,expected_v03", [
    ("response", "unclear", 0, 1), ("response", "fail", 0, 0),
    ("safety", "fail", 1, 0), ("safety", "unclear", 1, 1),
])
def test_protocol_gate_rules(gate, status, expected_v02, expected_v03):
    for standard, expected in (("0.2-simplified", expected_v02), ("0.3", expected_v03)):
        data = make_snapshot([[2, 3]], standard)
        data["results"][0][f"answer1_{gate}_gate"] = status
        tables = report(data)
        assert record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["配对有效题数"] == expected
        gate_row = record(tables, "A．", 对象="产品1", 指标=("响应" if gate == "response" else "安全") + "Gate" + stats.STATE_NAMES[status])
        assert gate_row["数量"] == 1


@pytest.mark.parametrize("bad_score", [None, True, "3", float("nan"), float("inf"), -1, 6, 2.5])
def test_invalid_scores_are_not_imputed_or_treated_as_ties(bad_score):
    data = make_snapshot([[bad_score, 3]])
    tables = report(data)
    pair = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert pair["配对有效题数"] == 0
    assert pair["分位值 A/B"] is None
    assert record(tables, "C2．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["平率"] is None
    completeness = record(tables, "B1．", 对象="理解需求 / 产品1", 指标="应评分输出完整率")
    assert completeness["数量"] == 0 and completeness["分母"] == 1


def test_na_and_unverifiable_do_not_become_product_failures():
    data = make_snapshot([[4, 5], [4, 5], [4, 5]])
    data["results"][0]["understanding_applicable"] = False
    data["results"][1]["understanding_verification_status"] = "unverifiable"
    data["results"][2]["has_conflict"] = "unclear"
    tables = report(data)
    row = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert row["适用题数"] == 2 and row["配对有效题数"] == 1
    assert row["A均分"] == 4
    assert record(tables, "B1．", 对象="理解需求", 指标="维度适用率")["占比"] == pytest.approx(2 / 3)


def test_zero_denominator_v03_preserves_zero_scores_and_gsb():
    tables = report(make_snapshot([[0, 0], [3, 0]], "0.3"))
    ab = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    ba = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品2 / 产品1"})
    assert ab["配对有效题数"] == 2 and ab["分位值 A/B"] is None
    assert "对照均分为0" in ab["说明"]
    assert ba["分位值 A/B"] == 0
    assert record(tables, "C2．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})["GSB数量"] == "1/1/0"


def test_latest_results_failures_pending_and_partial_are_accounted_separately():
    data = make_snapshot([[5, 5], [3, 3], [3, 3], [3, 3]])
    data["results"] = [data["results"][0], data["results"][1], {"index": 2, "error": "timeout", "error_type": "timeout"},
                       {**data["results"][0], "answer1_understanding_score": 2}]
    data["results"][1]["answer1_input_status"] = "partial"
    tables = report(data)
    assert record(tables, "A．", 对象="任务", 指标="结构化结果成功／覆盖率")["数量"] == 2
    assert record(tables, "A．", 对象="任务", 指标="技术失败")["数量"] == 1
    assert record(tables, "A．", 对象="任务", 指标="未完成")["数量"] == 1
    pair = record(tables, "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert pair["A均分"] == 2.5 and pair["配对有效题数"] == 2
    assert pair["受限证据题数"] == 1
    data["summary"] = {"failed": 999, "done": 888}
    legacy = history.export_rows(data)["汇总指标"][0]
    assert legacy["failed"] == 1 and legacy["unfinished"] == 1 and legacy["done"] == 2
    assert data["summary"]["failed"] == 999


def test_live_summary_does_not_count_unfinished_as_failed():
    from auto_eval.web.runner import _summarize
    from auto_eval.web.tasks import Task
    data = make_snapshot([[4, 5], [3, 3], [3, 3]])
    results = [data["results"][0], {"index": 1, "error": "timeout"}]
    task = Task(id="t", mode="compare", items=data["items"], results=results, options={})
    summary = _summarize(task)
    assert summary["done"] == summary["failed"] == summary["unfinished"] == 1


def test_protocol_and_product_cohorts_are_never_pooled():
    data = make_snapshot([[4, 5], [4, 5], [3, 3, 3]], count=2)
    data["items"][2]["product_count"] = 3
    data["results"][2]["product_count"] = 3
    data["results"][1].update(standard_version="0.3", bundle_revision="0.3.1")
    tables = report(data)
    assert len([t for t in tables if t.title.startswith("标准 ")]) == 3
    # An unknown revision must not silently adopt today's policy.
    data["results"][0]["bundle_revision"] = "9.9"
    assert any(t.title == "评分统计不可用" for t in report(data))


def test_subgroups_and_no_human_accuracy_fabrication():
    data = make_snapshot([[5, 3], [2, 5]])
    data["items"][0].update(query_images=["question.png"], evidence_mode="long_screenshot", category="旅游")
    data["items"][1]["category"] = "购物"
    tables = report(data)
    row = record(tables, "E．题型：图文题", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert row["配对有效题数"] == 1 and row["分位值 A/B"] == pytest.approx(5 / 3)
    assert all("未开展" in row[2] for row in table(tables, "F．").rows)


def test_bootstrap_preserves_pairing_reproducibility_and_query_groups():
    data = make_snapshot([[5, 3], [3, 5], [4, 2], [3, 2]])
    first = record(report(data), "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    second = record(report(data), "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert first == second
    assert first["分差95%下限"] <= first["均分差 A-B"] <= first["分差95%上限"]
    for item in data["items"]:
        item["session_group"] = "one-session"
    row = record(report(data), "C1．", 维度="理解需求", **{"方向 A/B": "产品1 / 产品2"})
    assert row["分差95%下限"] is None and "不足2个独立题组" in row["说明"]


@pytest.mark.parametrize("standard,count", [("0.2-simplified", 2), ("0.2-simplified", 3), ("0.3", 2), ("0.3", 3)])
def test_final_xlsx_has_one_statistics_sheet_with_numeric_percentages(standard, count):
    data = make_snapshot([[2, 3, 1][:count], [3, 2, 3][:count]], standard)
    data["dataset_name"] = "数据<&>\x00.xlsx"
    before = copy.deepcopy(data)
    with zipfile.ZipFile(BytesIO(history.build_xlsx(data))) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))
        sheets = ET.fromstring(archive.read("xl/workbook.xml")).findall("s:sheets/s:sheet", NS)
        names = [sheet.attrib["name"] for sheet in sheets]
        assert names[:2] == ["数据集明细", "逐题结果"]
        assert names.count(stats.SHEET_NAME) == 1
        number = names.index(stats.SHEET_NAME) + 1
        worksheet = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
        assert worksheet.find("s:sheetViews/s:sheetView/s:pane", NS).attrib["state"] == "frozen"
        style_xml = ET.fromstring(archive.read("xl/styles.xml"))
        cell_styles = style_xml.find("s:cellXfs", NS)
        assert len(cell_styles) == int(cell_styles.attrib["count"])
        percent_cells = [cell for cell in worksheet.findall(".//s:c", NS)
                         if cell_styles[int(cell.attrib["s"])].attrib["numFmtId"] == "10" and cell.find("s:v", NS) is not None]
        assert percent_cells and all(cell.get("t") != "inlineStr" for cell in percent_cells)
        assert all(math.isfinite(float(cell.find("s:v", NS).text)) for cell in percent_cells)
        text = "".join(worksheet.itertext())
        assert "GSB数量" in text and "分位值 A/B" in text
        assert ("产品3 / 产品1" in text) == (count == 3)
        assert "#DIV/0!" not in text and "#VALUE!" not in text
    assert data == before


def test_non_compare_and_empty_compare_exports():
    data = {"mode": "rich_content", "items": [], "results": []}
    with zipfile.ZipFile(BytesIO(history.build_xlsx(data))) as archive:
        assert stats.SHEET_NAME not in archive.read("xl/workbook.xml").decode()
    data["mode"] = "compare"
    with zipfile.ZipFile(BytesIO(history.build_xlsx(data))) as archive:
        assert stats.SHEET_NAME in archive.read("xl/workbook.xml").decode()
