"""Source metadata survives every result row without changing evaluation inputs."""
import csv
from io import BytesIO, StringIO
import json
from xml.etree import ElementTree as ET
import zipfile

import pytest

from auto_eval.web import history, server
from auto_eval.web.tasks import Task


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _workbook_rows(snapshot, number):
    with zipfile.ZipFile(BytesIO(history.build_xlsx(snapshot))) as archive:
        root = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
    rows = root.findall("s:sheetData/s:row", NS)
    values = [["".join(cell.itertext()) for cell in row] for row in rows]
    return values[0], [dict(zip(values[0], row)) for row in values[1:]], rows


def _parse(objects, mode, document_format="jsonl"):
    content = (
        json.dumps(objects, ensure_ascii=False, indent=2)
        if document_format == "json"
        else "\n".join(json.dumps(obj, ensure_ascii=False) for obj in objects)
    )
    response = server.api_parse(server.ParseReq(mode=mode, jsonl=content))
    assert response["errors"] == []
    assert response["count"] == len(objects)
    return response["items"]


def _source(case_index, mode, **extra):
    media = {"video1": "a.mp4", "video2": "b.mp4"} if mode == "compare" else {"video_path": "a.mp4"}
    return {"id": f"case-{case_index}", "query": f"问题{case_index}", **media, **extra}


@pytest.mark.parametrize("mode", ["compare", "rich_content"])
@pytest.mark.parametrize("document_format", ["jsonl", "json"])
def test_upload_to_history_export_keeps_extra_fields_by_original_index(mode, document_format):
    objects = [
        _source(0, mode, sessionid="000012345678901234567890", count=0, enabled=False,
                absent=None, metadata={"label": "中文", "list": [0, False, None]},
                tags=["first", {"nested": True}], formula="=HYPERLINK(\"https://example.invalid\")",
                long_integer=123456789012345678901),
        _source(1, mode, sessionid="000002", later_field="failed row"),
        _source(2, mode, session_id="upstream-id", last_field="not evaluated"),
    ]
    items = _parse(objects, mode, document_format)
    # Extra fields remain source metadata, not normalized judge/scheduler input.
    assert "sessionid" not in items[0] and "metadata" not in items[0]
    assert "session_group" not in items[2]
    task = Task(id="extra-fields-history", mode=mode, items=items, options={}, results=[
        {"index": 1, "error": "provider failure"},
        {"index": 0, "rationale": "completed", "problem_solved": "ok"},
    ])
    # Exercise exactly the JSON serialization used for persisted historical data.
    snapshot = json.loads(json.dumps(history.task_to_snapshot(task)))
    before = json.dumps(snapshot)
    sheets = history.export_rows(snapshot)
    extra_headers = ["sessionid", "count", "enabled", "absent", "metadata", "tags", "formula",
                     "long_integer", "later_field", "session_id", "last_field"]
    for sheet_name in ("数据集明细", "逐题结果"):
        rows = sheets[sheet_name]
        assert len(rows) == 3
        assert list(rows[0])[-len(extra_headers):] == extra_headers
        assert [row["sessionid"] for row in rows] == [objects[0]["sessionid"], "000002", ""]
        assert rows[0]["count"] == 0 and rows[0]["enabled"] is False
        assert rows[0]["absent"] == "null" and rows[1]["absent"] == ""
        assert json.loads(rows[0]["metadata"]) == objects[0]["metadata"]
        assert json.loads(rows[0]["tags"]) == objects[0]["tags"]
        assert rows[0]["long_integer"] == str(objects[0]["long_integer"])
        assert rows[1]["later_field"] == "failed row"
        assert rows[2]["session_id"] == "upstream-id"
        assert rows[2]["last_field"] == "not evaluated"
        csv_rows = list(csv.DictReader(StringIO(history.rows_to_csv(rows))))
        assert csv_rows[0]["sessionid"] == objects[0]["sessionid"]
        assert csv_rows[0]["count"] == "0" and csv_rows[0]["enabled"] == "False"
        assert csv_rows[0]["absent"] == "null"
        assert json.loads(csv_rows[0]["metadata"]) == objects[0]["metadata"]

    for number in (1, 2):
        headers, rows, xml_rows = _workbook_rows(snapshot, number)
        for key in ("sessionid", "formula", "long_integer"):
            column = headers.index(key)
            assert xml_rows[1][column].get("t") == "inlineStr"
            assert xml_rows[1][column].find("s:f", NS) is None
        assert rows[0]["sessionid"] == objects[0]["sessionid"]
        assert rows[0]["formula"] == objects[0]["formula"]
        assert rows[0]["long_integer"] == str(objects[0]["long_integer"])
        assert rows[1]["later_field"] == "failed row"
        assert rows[2]["last_field"] == "not evaluated"

    for index in (0, 1, 2):
        result = {"error": "provider failure"} if index == 1 else {}
        single = history.result_export_row(mode, result, index, snapshot["items"])
        assert {key: single[key] for key in extra_headers} == {
            key: sheets["逐题结果"][index][key] for key in extra_headers
        }
    assert json.dumps(snapshot) == before, "export must not modify historical input or results"


def test_collision_mapping_is_global_and_product3_like_metadata_is_not_filtered():
    objects = [
        _source(0, "compare", query_id="source-id", **{
            "输入字段.query_id": "literal prefix",
            "输入字段.query_id（2）": "literal suffix",
            "产品1抽帧数量": "source frame count",
            "产品3备注": "business metadata", "video3_sessionid": "not a third product",
            "产品3背景": "not the actual third product context", "error": "source error field",
            "数据集序号": "upstream order", "输入图片原图": "user source text",
        }),
        _source(1, "compare", query_id="source-id-2"),
    ]
    snapshot = {"mode": "compare", "items": _parse(objects, "compare"), "results": [
        {"index": 1, "error": "actual failure"}, {"index": 0, "item_id": "case-0"},
    ]}
    expected = {
        "输入字段.query_id（3）": "source-id",
        "输入字段.query_id": "literal prefix",
        "输入字段.query_id（2）": "literal suffix",
        "输入字段.产品1抽帧数量": "source frame count",
        "产品3备注": "business metadata", "video3_sessionid": "not a third product",
        "输入字段.产品3背景": "not the actual third product context",
        "输入字段.error": "source error field", "输入字段.数据集序号": "upstream order",
        "输入字段.输入图片原图": "user source text",
    }
    sheets = history.export_rows(snapshot)
    for sheet_name in ("数据集明细", "逐题结果"):
        assert {key: sheets[sheet_name][0][key] for key in expected} == expected
        assert sheets[sheet_name][1]["输入字段.query_id（3）"] == "source-id-2"
    assert sheets["逐题结果"][0]["query_id"] == "case-0"
    assert sheets["数据集明细"][0]["产品1抽帧数量"] == 0
    for number in (1, 2):
        headers, rows, _ = _workbook_rows(snapshot, number)
        assert {key: rows[0][key] for key in expected} == expected
        assert "产品3理解需求分" not in headers
        assert "产品3背景" not in headers
    single = history.result_export_row("compare", {"error": "actual failure"}, 0, snapshot["items"])
    assert single["error"] == "actual failure"
    assert {key: single[key] for key in expected} == expected


def test_custom_fields_are_read_from_original_source_not_runtime_or_judge_output():
    items = _parse([_source(0, "compare", sessionid="original")], "compare")
    items[0]["sessionid"] = "runtime value"
    snapshot = {"mode": "compare", "items": items,
                "results": [{"index": 0, "sessionid": "judge output"}]}
    sheets = history.export_rows(snapshot)
    assert sheets["数据集明细"][0]["sessionid"] == "original"
    assert sheets["逐题结果"][0]["sessionid"] == "original"
    assert history.result_export_row("compare", snapshot["results"][0], 0, items)["sessionid"] == "original"


def test_legacy_items_without_source_data_keep_custom_fields_but_not_runtime_frames():
    snapshot = {"mode": "compare", "items": [
        {"id": "legacy", "query": "old", "video1": "a.mp4", "video2": "b.mp4",
         "sessionid": "000-legacy", "frames1": ["runtime-only.jpg"], "frame_count": 1},
    ], "results": []}
    row = history.export_rows(snapshot)["逐题结果"][0]
    assert row["sessionid"] == "000-legacy"
    assert "frames1" not in row and "frame_count" not in row


def test_legacy_parser_bookkeeping_does_not_create_extra_result_columns():
    snapshot = {"mode": "compare", "items": [
        {"id": "legacy", "query": "old", "video1": "a.mp4", "video2": "b.mp4",
         "source_line": 2, "session_group": "csv-sess-0", "turn_index": 0},
    ], "results": []}
    sheets = history.export_rows(snapshot)
    assert history._extra_input_columns(snapshot) == {}
    row = sheets["逐题结果"][0]
    for key in ("source_line", "session_group", "turn_index"):
        assert key not in row and f"输入字段.{key}" not in row
    # Keep the pre-existing dataset bookkeeping columns for older snapshots.
    assert sheets["数据集明细"][0]["source_line"] == 2
    assert sheets["数据集明细"][0]["session_group"] == "csv-sess-0"
    assert sheets["数据集明细"][0]["turn_index"] == 0


def test_explicit_source_fields_named_like_parser_bookkeeping_still_export():
    items = _parse([_source(0, "compare", source_line="business-line",
                           session_group="business-group", turn_index=False)], "compare")
    sheets = history.export_rows({"mode": "compare", "items": items, "results": []})
    assert items[0]["source_line"] == 1
    for name in ("数据集明细", "逐题结果"):
        row = sheets[name][0]
        assert row["输入字段.source_line"] == "business-line"
        assert row["session_group"] == "business-group"
        assert row["turn_index"] is False


def test_known_evaluation_fields_do_not_repeat_as_extra_result_columns():
    items = _parse([_source(0, "compare", answer1="first", answer2="second", category="default",
                          task_start_time=0, sessionid="kept")], "compare")
    row = history.export_rows({"mode": "compare", "items": items, "results": []})["逐题结果"][0]
    assert row["sessionid"] == "kept"
    assert not {"id", "query", "video1", "video2", "answer1", "answer2", "category", "task_start_time"} & row.keys()


def test_csv_session_identifier_exports_without_changing_csv_grouping():
    content = (
        "query,is_start,is_end,文件路径,index,session_id,custom\n"
        "first,1,0,,0001,upstream-same,alpha\n"
        "second,0,1,a.mp4,0002,upstream-other,beta\n"
    )
    parsed = server.api_parse(server.ParseReq(mode="rich_content", csv=content))
    assert parsed["errors"] == []
    items = parsed["items"]
    assert items[0]["session_group"] == items[1]["session_group"] == "csv-sess-0"
    snapshot = {"mode": "rich_content", "items": items, "results": []}
    rows = history.export_rows(snapshot)["逐题结果"]
    assert [row["query_id"] for row in rows] == ["0001", "0002"]
    assert [row["session_id"] for row in rows] == ["upstream-same", "upstream-other"]
    assert [row["custom"] for row in rows] == ["alpha", "beta"]
    assert not {"index", "is_start", "is_end", "文件路径"} & rows[0].keys()


def test_json_csv_control_names_and_other_mode_fields_are_still_extra_metadata():
    items = _parse([_source(0, "rich_content", index="upstream-index", is_start=False,
                           answer1="not a rich-content judge input", sessionid="kept")], "rich_content")
    assert "session_group" not in items[0]
    row = history.export_rows({"mode": "rich_content", "items": items, "results": []})["逐题结果"][0]
    assert row["index"] == "upstream-index"
    assert row["is_start"] is False
    assert row["answer1"] == "not a rich-content judge input"


def test_extra_fields_do_not_overwrite_query_image_columns():
    source = _source(0, "compare", query_images=["missing-question.png"], **{
        "题型": "user label", "提问图片": "user picture field", "输入图片原图": "user original field",
        "query_image_meta": {"source": True}, "input_manifest_sha256": "source-hash",
    })
    items = _parse([source], "compare")
    items[0]["input_manifest_sha256"] = "runtime-hash"
    snapshot = {"mode": "compare", "items": items, "results": []}
    sheets = history.export_rows(snapshot)
    row = sheets["逐题结果"][0]
    assert row["题型"] == "图文题"
    assert row["输入字段.题型"] == "user label"
    assert row["输入字段.提问图片"] == "user picture field"
    assert row["输入字段.input_manifest_sha256"] == "source-hash"
    assert row["输入指纹"] == "runtime-hash"
    for number in (1, 2):
        headers, rows, _ = _workbook_rows(snapshot, number)
        assert "输入图片原图" in headers
        assert rows[0]["输入字段.输入图片原图"] == "user original field"
        assert json.loads(rows[0]["输入字段.query_image_meta"]) == {"source": True}
