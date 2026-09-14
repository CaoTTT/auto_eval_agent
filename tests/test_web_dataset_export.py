import json
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from auto_eval.web import history, server


@pytest.mark.parametrize("mode", ["compare", "rich_content"])
@pytest.mark.parametrize("answer", ["正常回答\n第二行\t<&> 🚗", "回答\x00\x0b\x1f结束", "回答\ud800\ufffe结束"])
def test_video_xlsx_keeps_all_rows_with_unusual_text(mode, answer):
    items = [
        {"id": f"video-{index}", "query": f"问题 {index}",
         "video1": "a.mp4", "video2": "b.mp4", "video_path": "a.mp4",
         "answer1": answer, "answer_text": answer}
        for index in range(25)
    ]
    data = {"mode": mode, "status": "done", "items": items, "results": [
        {"index": index, "query": items[index]["query"], "answer1": answer, "answer_text": answer}
        for index in reversed(range(25))
    ]}
    before = json.dumps(data)
    with zipfile.ZipFile(BytesIO(history.build_xlsx(data))) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ET.fromstring(archive.read(name))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for number, query_column in ((1, "query"), (2, "题目" if mode == "compare" else "Query")):
            sheet = ET.fromstring(archive.read(f"xl/worksheets/sheet{number}.xml"))
            rows = sheet.findall("s:sheetData/s:row", ns)
            assert len(rows) == 26, "all 25 cases must be exported, regardless of UI pagination"
            values = [["".join(cell.itertext()) for cell in row] for row in rows]
            column = values[0].index(query_column)
            assert [row[column] for row in values[1:]] == [item["query"] for item in items]
            expected = answer
            for character in ("\x00", "\x0b", "\x1f", "\ud800", "\ufffe"):
                expected = expected.replace(character, f"\\u{ord(character):04x}")
            answer_column = values[0].index("answer1" if number == 1 else "产品1回答") if mode == "compare" else values[0].index("answer_text")
            assert all(row[answer_column] == expected for row in values[1:])
    assert json.dumps(data) == before, "export must not change persisted evaluation data"


def _snapshot(project: Path) -> dict:
    video_1 = project / "data" / "videos" / "one.mp4"
    video_2 = project / "data" / "videos" / "two.mp4"
    frame_dir = project / "runs" / "videos" / "imported" / "session" / "001_op_1"
    frame_1 = frame_dir / "kf_001.jpg"
    frame_2 = frame_dir / "kf_002.jpg"
    video_1.parent.mkdir(parents=True)
    frame_dir.mkdir(parents=True)
    video_1.write_bytes(b"video")
    video_2.write_bytes(b"video")
    frame_1.write_bytes(b"frame-1")
    frame_2.write_bytes(b"frame-2")
    (frame_dir / "keyframes.json").write_text(
        json.dumps({
            "video": str(video_1),
            "selected": [
                {"index": 1, "time": 1.5, "source": "scene", "keep_reason": "scene-change"},
                {"index": 2, "time": 3.0, "source": "terminal", "keep_reason": "final-frame"},
            ],
        }),
        encoding="utf-8",
    )
    return {
        "task_id": "task-1",
        "dataset_name": "operation_cases.jsonl",
        "mode": "operation",
        "items": [
            {
                "id": "op_1",
                "query": "打开设置",
                "source_line": 3,
                "source_data": {
                    "id": "op_1",
                    "query": "打开设置",
                    "分享链接": "https://example.test/1",
                    "video_path": "data/videos/one.mp4",
                    "custom_field": "kept",
                },
                "video_path": str(video_1),
                "frames": [str(frame_1), str(frame_2)],
                "frame_count": 2,
                "duration": 4.5,
            },
            {
                "id": "op_2",
                "query": "关闭设置",
                "source_line": 4,
                "source_data": {
                    "id": "op_2",
                    "query": "关闭设置",
                    "分享链接": "",
                    "video_path": "data/videos/two.mp4",
                },
                "video_path": str(video_2),
                "frames": [],
                "frame_count": 0,
            },
            {
                "id": "op_3",
                "query": "打开蓝牙",
                "source_line": 5,
                "source_data": {
                    "id": "op_3",
                    "query": "打开蓝牙",
                    "video_path": "data/videos/missing.mp4",
                },
                "video_path": str(project / "data" / "videos" / "missing.mp4"),
            },
        ],
        # 故意使用与输入不同的完成顺序，并让第三条保持无结果。
        "results": [
            {
                "index": 1,
                "item_id": "op_2",
                "query": "关闭设置",
                "error": "provider failed",
            },
            {
                "index": 0,
                "item_id": "op_1",
                "query": "打开设置",
                "correctness": "right",
                "total": 5,
                "rubric": {"操作完成度": 5},
            },
        ],
        "summary": {},
        "item_progress": {},
    }


def test_export_keeps_source_fields_paths_and_input_alignment(
    tmp_path: Path,
    monkeypatch,
):
    snapshot = _snapshot(tmp_path)
    monkeypatch.setattr(history, "PROJECT_ROOT", tmp_path)

    sheets = history.export_rows(snapshot)

    dataset = sheets["数据集明细"]
    assert len(dataset) == 3
    assert dataset[0]["分享链接"] == "https://example.test/1"
    assert dataset[0]["custom_field"] == "kept"
    assert dataset[0]["录屏项目相对路径"] == "data/videos/one.mp4"
    assert dataset[0]["抽帧目录项目相对路径"] == (
        "runs/videos/imported/session/001_op_1"
    )
    assert dataset[0]["帧项目相对路径"].splitlines() == [
        "runs/videos/imported/session/001_op_1/kf_001.jpg",
        "runs/videos/imported/session/001_op_1/kf_002.jpg",
    ]

    results = sheets["逐题结果"]
    assert [row["item_id"] for row in results] == ["op_1", "op_2", "op_3"]
    assert results[0]["correctness"] == "right"
    assert results[1]["评估状态"] == "评估失败"
    assert "correctness" not in results[1]
    assert results[2]["评估状态"] == "待评估"
    assert "correctness" not in results[2]

    frames = sheets["抽帧清单"]
    assert frames[0]["时间点"] == 1.5
    assert frames[0]["保留原因"] == "scene-change"
    assert frames[-1]["抽帧状态"] == "无抽帧结果"


def test_write_frames_zip_contains_images_and_manifest(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    archive = tmp_path / "export.zip"

    history.write_frames_zip(snapshot, archive, project_root=tmp_path)

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "001_op_1/kf_001.jpg" in names
        assert "001_op_1/kf_002.jpg" in names
        assert "001_op_1/keyframes.json" in names
        manifest = [
            json.loads(line)
            for line in zf.read("manifest.jsonl").decode("utf-8").splitlines()
        ]
        assert manifest[0]["video_project_path"] == "data/videos/one.mp4"
        assert manifest[0]["timestamp"] == 1.5
        assert manifest[0]["keep_reason"] == "scene-change"
        assert any(
            row["id"] == "op_2" and row["status"] == "missing"
            for row in manifest
        )
        metadata = json.loads(zf.read("001_op_1/keyframes.json"))
        assert metadata["video"] == "data/videos/one.mp4"


def test_write_frames_zip_can_export_one_dataset_item(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    archive = tmp_path / "single-item.zip"

    history.write_frames_zip(
        snapshot,
        archive,
        project_root=tmp_path,
        item_indexes={0},
    )

    with zipfile.ZipFile(archive) as zf:
        assert "001_op_1/kf_001.jpg" in zf.namelist()
        assert not any(name.startswith("002_") for name in zf.namelist())
        manifest = [
            json.loads(line)
            for line in zf.read("manifest.jsonl").decode("utf-8").splitlines()
        ]
        assert {row["id"] for row in manifest} == {"op_1"}


def test_load_item_judge_calls_matches_task_and_item_index(tmp_path: Path):
    snapshot = _snapshot(tmp_path)
    snapshot.update({
        "task_id": "task-1",
        "session_name": "session-1",
    })
    trace = tmp_path / "runs" / "judge_calls_operation.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "task_id": "task-1",
            "session_name": "session-1",
            "item_index": 0,
            "item_id": "op_1",
            "judge": "judge_2",
            "model_raw_output": "raw-2",
        },
        {
            "task_id": "task-1",
            "session_name": "session-1",
            "item_index": 0,
            "item_id": "op_1",
            "judge": "judge_1",
            "model_raw_output": "raw-1",
        },
        {
            "task_id": "task-1",
            "session_name": "session-1",
            "item_index": 1,
            "item_id": "op_2",
            "judge": "judge_2",
        },
        {
            "task_id": "other-task",
            "item_index": 0,
            "item_id": "op_1",
            "judge": "judge_2",
        },
    ]
    trace.write_text(
        "".join(json.dumps(row) + "\n" for row in records),
        encoding="utf-8",
    )

    payload = history.load_item_judge_calls(
        snapshot,
        0,
        runs_dir=trace.parent,
        project_root=tmp_path,
        trace_paths=[trace],
    )

    assert payload["item_id"] == "op_1"
    assert payload["judge_call_count"] == 2
    assert {
        row["model_raw_output"] for row in payload["judge_calls"]
    } == {"raw-1", "raw-2"}
    assert payload["judge_calls"][0]["_trace_file"] == (
        "runs/judge_calls_operation.jsonl"
    )


def test_item_judge_export_supports_chinese_download_filename(monkeypatch):
    snapshot = {
        "task_id": "task-1",
        "mode": "operation",
        "items": [{"id": "众测_001", "query": "打开设置"}],
        "results": [],
    }
    monkeypatch.setattr(server, "get_task", lambda _: None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: snapshot)
    monkeypatch.setattr(
        server,
        "load_item_judge_calls",
        lambda _snapshot, _index: {
            "item_id": "众测_001",
            "judge_call_count": 1,
            "judge_calls": [{"model_raw_output": "raw"}],
        },
    )

    response = server.api_export_item("task-1", 0, "judge_calls")

    assert response.status_code == 200
    assert json.loads(response.body)["judge_call_count"] == 1
    disposition = response.headers["content-disposition"]
    assert "filename*=UTF-8''" in disposition
    disposition.encode("latin-1")
