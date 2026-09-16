"""JSON documents and JSONL share validation and preserve original case fields."""
import copy
import json

import pytest

from auto_eval.web.parse_input import parse_csv, parse_jsonl
from auto_eval.web.runner import _to_evalitem


def case(mode, item_id="case-1", **extra):
    row = {"id": item_id, "query": "  测试问题  "}
    row.update(
        {"video1": "a.mp4", "video2": "b.mp4"}
        if mode == "compare"
        else {"video_path": "a.mp4"}
    )
    return {**row, **extra}


@pytest.mark.parametrize("mode", ["compare", "rich_content"])
@pytest.mark.parametrize("indent", [None, 2])
def test_json_array_keeps_extra_values_in_source_only(mode, indent):
    source = [
        case(mode, sessionid="session-A", custom={"region": "上海", "ids": [1, 2]},
             enabled=False, count=0, optional=None, session_group="untrusted",
             turn_index=7, metadata={"custom": "source-only"}),
        case(mode, "case-2", sessionid="session-A"),
    ]
    before = copy.deepcopy(source)
    items, errors = parse_jsonl(json.dumps(source, ensure_ascii=False, indent=indent), mode)

    assert errors == []
    assert [item["id"] for item in items] == ["case-1", "case-2"]
    assert [item["source_line"] for item in items] == [1, 2]
    assert [item["source_data"] for item in items] == source == before
    assert items[0]["query"] == "测试问题"
    for item in items:
        assert not {"sessionid", "session_group", "turn_index", "metadata"} & item.keys()
        model_input = _to_evalitem(item, 0).model_dump()
        assert model_input["metadata"] == {}
        assert "source-only" not in json.dumps(model_input)
        assert "session-A" not in json.dumps(model_input)


@pytest.mark.parametrize("mode", ["compare", "rich_content"])
def test_pretty_single_object_keeps_actual_start_line_and_bom(mode):
    source = case(mode, session_id="original-session", note={"comment": "保留"})
    content = "\ufeff\n\n" + json.dumps(source, ensure_ascii=False, indent=2) + "\n"
    items, errors = parse_jsonl(content, mode)
    assert errors == []
    assert len(items) == 1
    assert items[0]["source_line"] == 3
    assert items[0]["source_data"] == source


@pytest.mark.parametrize("mode", ["compare", "rich_content"])
def test_array_reports_invalid_records_by_position_and_keeps_valid_records(mode):
    missing_media = case(mode, "missing-media")
    missing_media.pop("video2" if mode == "compare" else "video_path")
    rows = [case(mode), None, {"query": 123}, missing_media,
            case(mode, "case-2"), case(mode)]
    items, errors = parse_jsonl(json.dumps(rows, indent=2), mode)
    assert [item["id"] for item in items] == ["case-1", "case-2"]
    assert [item["source_line"] for item in items] == [1, 5]
    assert len(errors) == 4
    for message, position in zip(errors, [2, 3, 4, 6]):
        assert message.startswith(f"第 {position} 条（JSON 数组）")
    assert "必须是 JSON 对象" in errors[0]
    assert "缺少 question" in errors[1]
    assert "id 重复" in errors[3]


def test_json_array_preserves_compare_screenshot_and_query_image_validation():
    rows = [
        {"query": "图文题", "screenshot1": "one.png", "screenshot2": "two.png",
         "query_images": ["question.png"], "sessionid": "one"},
        {"query": "混用证据", "video1": "one.mp4", "screenshot2": "two.png"},
        {"query": "图片类型错误", "video1": "one.mp4", "video2": "two.mp4",
         "query_images": "question.png"},
    ]
    items, errors = parse_jsonl(json.dumps(rows), "compare")
    assert len(items) == 1
    assert items[0]["evidence_mode"] == "long_screenshot"
    assert items[0]["input_modality"] == "text_image"
    assert items[0]["query_images"] == ["question.png"]
    assert errors[0].startswith("第 2 条（JSON 数组）")
    assert "混用" in errors[0]
    assert errors[1].startswith("第 3 条（JSON 数组）")


def test_empty_array_has_explicit_error_but_blank_jsonl_remains_compatible():
    assert parse_jsonl("[\n]", "compare") == ([], ["JSON 数组中没有数据项"])
    assert parse_jsonl(" \n\n", "compare") == ([], [])


def test_malformed_array_reports_document_line_and_column_without_partial_import():
    items, errors = parse_jsonl('[\n {"query":"q","video_path":"a.mp4"},\n@]', "rich_content")
    assert items == []
    assert len(errors) == 1
    assert "JSON 数组解析错误：第 3 行、第 1 列" in errors[0]


def test_jsonl_keeps_physical_lines_and_error_order():
    content = "\n".join([
        "", json.dumps(case("rich_content")), '{"query":3}', "not json", "",
        json.dumps(case("rich_content", "case-2", sessionid="second")),
    ])
    items, errors = parse_jsonl(content, "rich_content")
    assert [item["source_line"] for item in items] == [2, 6]
    assert [item["id"] for item in items] == ["case-1", "case-2"]
    assert errors[0] == "第 3 行缺少 question"
    assert errors[1].startswith("第 4 行 JSON 错误：")
    assert items[1]["source_data"]["sessionid"] == "second"


def test_jsonl_array_record_does_not_swallow_following_valid_object():
    content = "[]\n" + json.dumps(case("rich_content"))
    items, errors = parse_jsonl(content, "rich_content")
    assert len(items) == 1 and items[0]["source_line"] == 2
    assert errors == ["第 1 行必须是 JSON 对象"]


def test_csv_session_id_remains_passthrough_without_changing_multiturn_grouping():
    content = (
        "query,is_start,is_end,文件路径,sessionid\n"
        "第一轮,true,false,,shared\n"
        "第二轮,false,true,one.mp4,shared\n"
        "新会话,true,true,two.mp4,shared\n"
    )
    items, errors = parse_csv(content, "rich_content")
    assert errors == []
    assert [item["session_group"] for item in items] == ["csv-sess-0", "csv-sess-0", "csv-sess-1"]
    assert [item["turn_index"] for item in items] == [0, 1, 0]
    assert [item["source_data"]["sessionid"] for item in items] == ["shared"] * 3
    assert all("sessionid" not in item for item in items)
