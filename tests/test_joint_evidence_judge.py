import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from auto_eval.config import VisualModeProfile, VisualExtractionConfig
from auto_eval.judges.compare_protocols import list_compare_protocols, resolve_compare_protocol
from auto_eval.judges.joint_evidence_prompt import MODE, VERSION, adapt_joint_prompts
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.long_screenshot import prepare_long_screenshot
from auto_eval.web import history
from auto_eval.web.screenshot_evidence import evidence_records, write_evidence_zip
from test_vqa_input import Client


@pytest.fixture(params=[2, 3], ids=["two-products", "three-products"])
def joint_input(tmp_path, monkeypatch, request):
    from auto_eval import evidence_audit
    monkeypatch.setattr(evidence_audit, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    metas, aux, aux_meta = [], [], []
    item = {"id": "joint-case", "query": "路线规划", "product_count": request.param, "evidence_mode": MODE}
    for n in range(1, request.param + 1):
        screenshot = tmp_path / f"screenshot{n}.png"
        Image.new("RGB", (64, 128), "white").save(screenshot)
        meta = prepare_long_screenshot(screenshot, tmp_path / f"parts{n}", profile.long_screenshot)
        metas.append(meta)
        paths = []
        for i in (1, 2):
            frame = tmp_path / f"p{n}_frame{i}.png"
            Image.new("RGB", (720, 1280), "blue" if i == 1 else "green").save(frame)
            paths.append(str(frame))
        aux.append(paths)
        aux_meta.append([{"time_s": t, "source_video_sha256": f"source{n}", "sha256": "source-frame-hash", "keep_reason": "stable-state"} for t in (1.0, 2.0)])
        item.update({f"screenshot{n}": str(screenshot), f"screenshot_meta{n}": meta,
            f"frames{n}": [part["path"] for part in meta["slices"]],
            f"aux_frames{n}": paths, f"aux_frame_meta{n}": aux_meta[-1], f"video{n}": f"v{n}.mp4"})
    return profile, metas, aux, aux_meta, item


@pytest.mark.parametrize("protocol", list_compare_protocols(), ids=lambda p: p.standard_version)
def test_joint_adapter_replaces_conflicting_absence_rules_for_every_protocol(protocol):
    original = protocol.system_template.render(persona="test", product_count=2, evidence_mode="long_screenshot")
    system, user = adapt_joint_prompts(original, "query", [2, 2])
    assert "不得把“没有观察到”直接等同于“产品没有提供”" not in system
    assert "长截图没有覆盖某部分，只能标记partial或证据不足" not in system
    assert "以关键帧为主要证据" not in system
    assert "长截图和本产品全部录屏补充帧均未出现该组件，则判定未提供该组件" in system
    assert "service_closure仍适用" in system
    assert "产品1：2张补充录屏帧" in user
    assert original == protocol.system_template.render(persona="test", product_count=2, evidence_mode="long_screenshot")


async def run_joint(prepared, client=None):
    profile, metas, aux, aux_meta, item = prepared
    client = client or Client(count=item["product_count"])
    def save(record):
        item["screenshot_evidence"] = deepcopy(record)
        if record.get("request_budget_report"):
            item["request_budget_report"] = record["request_budget_report"]
    result = await VisualCompareJudge(client, profile).evaluate(
        question=item["query"], evidence_mode=MODE, screenshot_metas=metas,
        frames1=item["frames1"], frames2=item["frames2"], frames3=item.get("frames3"), aux_frame_groups=aux,
        aux_frame_metas=aux_meta, evidence_callback=save)
    return {"task_id": "joint-test", "mode": "compare", "items": [item], "results": [{"index": 0, **result}]}, client


async def test_one_call_sends_both_sources_per_product_and_audits_actual_encoded_bytes(joint_input, tmp_path):
    snapshot, client = await run_joint(joint_input)
    assert len(client.calls) == 1
    parts = client.calls[0][2]["content_parts"]
    record = evidence_records(snapshot)[0]
    count = joint_input[4]["product_count"]
    assert [im["product_no"] for im in record["images"]] == [n for n in range(1, count + 1) for _ in range(3)]
    assert [im.get("evidence_type") for im in record["images"]] == ["long_screenshot", "video_frame", "video_frame"] * count
    assert [im["time_s"] for im in record["images"] if "time_s" in im] == [1, 2] * count
    assert record["joint_evidence_version"] == VERSION
    assert record["request_budget_report"]["actual_image_count"] == 3 * count
    trace_images = client.calls[0][2]["image_metadata"]
    assert [im["sha256"] for im in trace_images] == [im["sha256"] for im in record["images"]]
    assert all(im["source_frame_sha256"] == "source-frame-hash" for im in record["images"] if im.get("evidence_type") == "video_frame")
    assert snapshot["results"][0]["input_manifest_sha256"] == record["request_sha256"]
    assert "base64," not in json.dumps(record)
    with zipfile.ZipFile(write_evidence_zip(snapshot, tmp_path / "evidence.zip")) as archive:
        images = json.loads(archive.read("manifest.json"))["records"][0]["images"]
        urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
        for image, url in zip(images, urls, strict=True):
            assert archive.read(image["archive_path"]) == base64.b64decode(url.split(",", 1)[1])


async def test_combined_request_exceeds_budget_without_sending_any_model_call(joint_input):
    profile, _, _, _, item = joint_input
    profile.query_images.max_input_tokens = 100
    client = Client()
    with pytest.raises(ValueError) as caught:
        await run_joint(joint_input, client)
    assert not client.calls
    assert caught.value.code == "context_budget_exceeded"
    report = caught.value.request_budget_report
    assert report["actual_image_count"] == 3 * item["product_count"] and report["blocked"]
    record = item["screenshot_evidence"]
    assert record["record_status"] == "blocked_before_model_call"
    assert record["retry_allowed"] is False
    assert record["context_limit_error"] and len(record["images"]) == 3 * item["product_count"]


async def test_declared_model_input_limit_is_enforced_with_trace(joint_input):
    from auto_eval.config import JudgeConfig
    client = Client()
    client.cfg = JudgeConfig(name="test", model="fake-model", max_input_tokens=100)
    with pytest.raises(ValueError) as caught:
        await run_joint(joint_input, client)
    assert not client.calls
    assert caught.value.request_budget_report["effective_limits"]["max_input_tokens"] == 100


async def test_json_repair_context_rejection_updates_audit_with_its_own_budget(joint_input):
    class RepairClient(Client):
        async def complete(self, system, user, **kwargs):
            self.calls.append((system, user, kwargs))
            return "broken JSON"

        async def repair_json(self, *_args, **_kwargs):
            exc = ValueError("maximum context length exceeded while repairing JSON")
            exc.request_budget_report = {"report_id": "repair-budget", "estimated_input_tokens": 123}
            raise exc

    client = RepairClient()
    with pytest.raises(ValueError, match="maximum context"):
        await run_joint(joint_input, client)
    record = joint_input[4]["screenshot_evidence"]
    assert len(client.calls) == 1
    assert record["record_status"] == "model_request_limit_rejected"
    assert record["failed_request_purpose"] == "json_repair"
    assert record["failed_request_budget_report"]["report_id"] == "repair-budget"
    assert record["context_limit_error"]["request_budget_report"]["report_id"] == "repair-budget"
    assert record["request_budget_report"]["actual_image_count"] == 3 * joint_input[4]["product_count"]
    assert record["retry_allowed"] is False
    joint_input[4]["request_budget_report"] = record["failed_request_budget_report"]
    exported_record = evidence_records({"items": [joint_input[4]]})[0]
    assert exported_record["request_budget_report"]["actual_image_count"] == 3 * joint_input[4]["product_count"]
    assert exported_record["failed_request_budget_report"]["report_id"] == "repair-budget"


async def test_joint_rejects_multi_turn_and_missing_or_unsorted_aux_metadata(joint_input):
    profile, metas, aux, aux_meta, item = joint_input
    judge = VisualCompareJudge(Client(), profile)
    kwargs = dict(question="q", evidence_mode=MODE, screenshot_metas=metas,
        frames1=item["frames1"], frames2=item["frames2"], frames3=item.get("frames3"), aux_frame_groups=aux, aux_frame_metas=aux_meta)
    with pytest.raises(ValueError, match="multiturn_video_unsupported"):
        await judge.evaluate(**kwargs, conversation_input={"turns": []})
    with pytest.raises(ValueError, match="元数据不完整"):
        await judge.evaluate(**{**kwargs, "aux_frame_metas": [aux_meta[0][:1], *aux_meta[1:]]})
    with pytest.raises(ValueError, match="按时间排序"):
        await judge.evaluate(**{**kwargs, "aux_frame_metas": [list(reversed(aux_meta[0])), *aux_meta[1:]]})


async def test_joint_history_exports_both_streams_and_budget_diagnostics(joint_input):
    snapshot, _ = await run_joint(joint_input)
    streams = history._item_visual_streams(snapshot["items"][0])
    assert [s["evidence_mode"] for s in streams] == ["long_screenshot", "video_frames"] * joint_input[4]["product_count"]
    assert streams[1]["frames"] == [Path(p) for p in joint_input[2][0]]
    sheets = history.export_rows(snapshot)
    assert "请求预算与错误" in sheets and "视觉证据清单" in sheets
    evidence_rows = sheets["长截图请求图片序列"]
    assert any(row["证据类型"] == "video_frame" and row["录屏时间（秒）"] == 1 for row in evidence_rows)


def test_budget_export_chunks_large_reports_without_losing_error_trace(tmp_path):
    report = {"report_id": "large-budget", "image_views": [{"metadata": "补充帧😀" * 10000}]}
    failure = {"error_id": "large-error", "provider_request_id": "provider-id", "reason": "超过上下文限制", "request_budget_report": report}
    snapshot = {"mode": "compare", "items": [{"id": "c1", "query": "q"}],
        "results": [{"index": 0, "error": "context exceeded", "request_id": "task-request", "retry_allowed": False,
            "request_budget_report": report, "context_limit_error": failure}]}
    rows = history.export_rows(snapshot)["请求预算与错误"]
    assert len(rows) > 1
    assert json.loads("".join(row["请求预算"] for row in rows)) == report
    assert json.loads("".join(row["请求限制错误"] for row in rows)) == failure
    assert all(row["请求编号"] == "task-request" and row["网关请求编号"] == "provider-id" for row in rows)
    assert all(len(row[key].encode("utf-16-le")) // 2 < 32767 for row in rows for key in ("请求预算", "请求限制错误"))
    from openpyxl import load_workbook
    target = tmp_path / "budget.xlsx"
    history.write_xlsx(snapshot, target, include_images=False)
    book = load_workbook(target, read_only=True)
    try:
        for sheet in book:
            assert all(len(str(cell.value or "").encode("utf-16-le")) // 2 < 32767 for row in sheet for cell in row)
        data = list(book["请求预算与错误"].values)
        budget_column = data[0].index("请求预算")
        assert json.loads("".join(row[budget_column] or "" for row in data[1:])) == report
    finally:
        book.close()
