import base64
import hashlib
import io
import itertools
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image, PngImagePlugin

from auto_eval.config import AppConfig, JudgeConfig, LongScreenshotConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval import long_screenshot as ls
from auto_eval.judges.base import JudgeClient, _usage_dict
from auto_eval.judges.compare_protocols import DIMENSIONS, resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.web import history, runner, server, video_prepare
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.tasks import Task, _task_from_snapshot


def profile(**limits):
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"), long_screenshot=LongScreenshotConfig(**limits))


def save_image(tmp_path, name="source.png", *, size=(64, 180), kind="safe"):
    pixels = np.full((size[1], size[0], 3), 255, dtype=np.uint8)
    if kind == "safe":
        for y in range(8, size[1] - 8, 24):
            pixels[y:y + 8, 8:-8] = 20
    elif kind == "fallback":
        pixels[:, 2:-2] = 200
    elif kind == "risky":
        pixels[:, 4:-4:4] = 0
        pixels[:, 5:-4:4] = 0
    elif kind == "noise":
        pixels = np.random.default_rng(123).integers(0, 256, pixels.shape, dtype=np.uint8)
    path = tmp_path / name
    Image.fromarray(pixels).save(path)
    return path


def assert_reconstruct(path, meta, cfg):
    with Image.open(path) as original:
        expected = np.asarray(original)
    arrays = []
    end = 0
    for part in meta["slices"]:
        assert part["start_y"] == end
        end = part["end_y"]
        with Image.open(part["path"]) as image:
            assert image.width == expected.shape[1]
            assert ls._fits(*image.size, part["data_url_bytes"], cfg)
            arrays.append(np.asarray(image).copy())
    assert end == expected.shape[0]
    np.testing.assert_array_equal(np.concatenate(arrays), expected)
    assert meta["overlap_pixels"] == 0 and meta["has_overlap"] is False


@pytest.mark.parametrize("suffix,mime", [("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp")])
def test_original_bytes_never_saved_or_resized(tmp_path, monkeypatch, suffix, mime):
    path = save_image(tmp_path, f"original.{suffix}")
    original = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("原图路径不得 save/resize")
    monkeypatch.setattr(Image.Image, "save", forbidden)
    monkeypatch.setattr(Image.Image, "resize", forbidden)
    cfg = LongScreenshotConfig()
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    url = ls.encode_original_image(path, cfg)
    assert url.startswith(f"data:{mime};base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == original
    assert meta["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert meta["original_data_url_bytes"] == len(url)
    assert meta["split_status"] == "original"
    assert [part["path"] for part in meta["slices"]] == [str(path)]
    assert not (tmp_path / "parts").exists()


@pytest.mark.parametrize("kind", ["safe", "fallback", "risky"])
def test_pixel_limit_minimum_parts_and_boundary_status(tmp_path, kind):
    path = save_image(tmp_path, kind=kind)
    cfg = LongScreenshotConfig(max_pixels=64 * 95)
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_count"] == 2
    assert meta["split_status"] == kind
    assert all(b["status"] == kind for b in meta["boundaries"])
    assert_reconstruct(path, meta, cfg)
    again = ls.prepare_long_screenshot(path, tmp_path / "again", cfg)
    assert again["boundaries"] == meta["boundaries"]


def test_safe_cut_global_not_greedy(tmp_path):
    pixels = np.zeros((180, 32, 3), dtype=np.uint8)
    pixels[:, ::3] = 255
    pixels[75:85] = 255
    pixels[110:120] = 255
    # 最后一个安全点115若贪心采用，将无法用两块覆盖；选80才能最少两块。
    path = tmp_path / "global.png"
    Image.fromarray(pixels).save(path)
    cfg = LongScreenshotConfig(max_pixels=32 * 105)
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_count"] == 2
    assert 77 <= meta["boundaries"][0]["y"] <= 83
    assert meta["split_status"] == "safe"
    assert_reconstruct(path, meta, cfg)


@pytest.mark.parametrize("limit,count", [(2800, 2), (1800, 3)])
def test_base64_limit_actual_png_and_minimum(tmp_path, limit, count):
    path = save_image(tmp_path, size=(24, 48), kind="noise")
    cfg = LongScreenshotConfig(max_data_url_bytes=limit)
    assert 24 * 48 < cfg.max_pixels
    assert ls.data_url_size(path.stat().st_size) >= cfg.max_data_url_bytes
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_count"] == count
    assert_reconstruct(path, meta, cfg)


def test_data_url_equal_limit_is_not_original(tmp_path):
    path = save_image(tmp_path, size=(24, 48), kind="noise")
    cfg = LongScreenshotConfig(max_data_url_bytes=ls.data_url_size(path.stat().st_size))
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_status"] != "original"
    assert_reconstruct(path, meta, cfg)


def test_oversized_png_metadata_can_fit_one_lossless_part(tmp_path):
    path = tmp_path / "metadata.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("note", "x" * 3000)
    Image.new("RGB", (24, 48), "white").save(path, pnginfo=info)
    cfg = LongScreenshotConfig(max_data_url_bytes=1000)
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_count"] == 1 and meta["split_status"] == "safe"
    assert_reconstruct(path, meta, cfg)


def test_jpeg_split_preserves_decoded_pixels(tmp_path):
    path = save_image(tmp_path, "source.jpg", kind="noise")
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", LongScreenshotConfig(max_pixels=64 * 95))
    assert all(Path(p["path"]).suffix == ".png" for p in meta["slices"])
    assert_reconstruct(path, meta, LongScreenshotConfig(max_pixels=64 * 95))


def test_aspect_ratio_limit_and_impossible_width(tmp_path):
    path = save_image(tmp_path, size=(12, 120))
    cfg = LongScreenshotConfig(max_aspect_ratio=5)
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_count"] == 2
    assert_reconstruct(path, meta, cfg)
    wide = save_image(tmp_path, "wide.png", size=(100, 11))
    with pytest.raises(ValueError, match="全宽"):
        ls.prepare_long_screenshot(wide, tmp_path / "wide", LongScreenshotConfig(max_pixels=1000))


@pytest.mark.parametrize("kind", ["small", "broken", "animated", "gif"])
def test_invalid_input_rejected(tmp_path, kind):
    path = tmp_path / "invalid.png"
    if kind == "small":
        Image.new("RGB", (10, 30)).save(path)
    elif kind == "broken":
        path.write_bytes(b"not a png")
    elif kind == "animated":
        Image.new("RGB", (32, 32)).save(path, save_all=True, append_images=[Image.new("RGB", (32, 32), "white")])
    else:
        Image.new("RGB", (32, 32)).save(path, format="GIF")
    with pytest.raises((ValueError, OSError)):
        ls.prepare_long_screenshot(path, tmp_path / "parts", LongScreenshotConfig())


def test_dp_matches_exhaustive_lexicographic_objective():
    height, low, high = 12, 2, 5
    risks = np.array([0, 3, 2, 1, 9, 3, 8, 6, 0, 2, 1, 8, 0], dtype=float)
    blocked = {5: {0}, 9: {4}}
    def cost(cuts):
        return (len(cuts)-1, sum(risks[y] for y in cuts[1:-1]), sum((b-a)**2 for a,b in zip(cuts,cuts[1:])), tuple(-y for y in cuts[1:-1]))
    possible = []
    for count in range(height):
        for boundaries in itertools.combinations(range(1, height), count):
            cuts = [0, *boundaries, height]
            if all(low <= b-a <= high and a not in blocked.get(b, set()) for a,b in zip(cuts,cuts[1:])):
                possible.append(cuts)
    result = ls._optimal_path(height, low, high, risks, blocked)
    assert cost(result) == min(map(cost, possible))


def test_encoded_edge_rejection_does_not_assume_monotonic_size(tmp_path, monkeypatch):
    path = save_image(tmp_path, size=(16, 36), kind="noise")
    cfg = LongScreenshotConfig(max_data_url_bytes=500)
    real_encode = ls._png_bytes
    # 第一条最优边失败；保留另一条更长可行边，防止错误二分/高度截断。
    def encoded(image, start, end):
        if (start, end) in {(0, 20), (20, 36)}:
            return real_encode(Image.new("RGB", image.size, "white"), start, end)
        return b"x" * 600
    monkeypatch.setattr(ls, "_png_bytes", encoded)
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert [(p["start_y"], p["end_y"]) for p in meta["slices"]] == [(0, 20), (20, 36)]


@pytest.mark.parametrize("count", [2, 3])
def test_jsonl_and_api_screenshots(count):
    item = {"query": "q", **{f"screenshot{n}": f"{n}.png" for n in range(1, count+1)}}
    items, errors = parse_jsonl(json.dumps(item), "compare")
    assert not errors and items[0]["product_count"] == count
    assert items[0]["evidence_mode"] == "long_screenshot"
    req = server.EvalReq(mode="compare", items=[item])
    server._validate_eval_request(req, AppConfig(judges=[JudgeConfig(name="test")]))
    assert req.items[0]["evidence_mode"] == "long_screenshot"


@pytest.mark.parametrize("updates", [
    {"video1": "a.mp4"}, {"screenshot2": None, "video2": "b.mp4"},
    {"product_count": 2, "screenshot3": "c.png"}, {"product_count": 2, "answer3": "c"},
    {"product_count": 2, "context3": "c"}, {"evidence_mode": "video_frames"},
    {"screenshot2": 42},
])
def test_jsonl_and_direct_api_reject_conflicts(updates):
    item = {"query": "q", "screenshot1": "a.png", "screenshot2": "b.png", **updates}
    assert parse_jsonl(json.dumps(item), "compare")[1]
    with pytest.raises(HTTPException):
        server._validate_eval_request(server.EvalReq(mode="compare", items=[item]), AppConfig(judges=[JudgeConfig(name="test")]))


def model_data(count=2):
    data = {"product_count": count, "needs_human_review": False, "review_reasons": []}
    for n in range(1, count+1):
        data.update({f"answer{n}_input_status": "complete", f"answer{n}_response_gate": "pass", f"answer{n}_safety_gate": "pass"})
    for dimension in DIMENSIONS:
        data[f"{dimension}_applicable"] = False
    return data


class FakeClient:
    persona = "test"
    model = "test-model"
    cfg = JudgeConfig(name="test")
    def __init__(self, count=2):
        self.calls = []
        self.count = count
    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        return json.dumps(model_data(self.count))
    async def aclose(self):
        pass


@pytest.mark.parametrize("protocol", ["0.2-simplified", "0.3"])
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("kind", ["safe", "fallback", "risky"])
async def test_judge_product_order_prompt_and_forced_review(tmp_path, monkeypatch, protocol, count, kind):
    def forbidden(*args, **kwargs):
        pytest.fail("长截图不得调用 encode_frame")
    monkeypatch.setattr("auto_eval.judges.visual_compare_judge.encode_frame", forbidden)
    cfg = profile(max_pixels=64*95)
    metas = []
    for n in range(count):
        path = save_image(tmp_path, f"{n}.png", kind=kind)
        metas.append(ls.prepare_long_screenshot(path, tmp_path / str(n), cfg.long_screenshot))
    client = FakeClient(count)
    judge = VisualCompareJudge(client, cfg, f"qa_competitor_compare@{protocol}")
    result = await judge.evaluate(question="q", product_count=count, evidence_mode="long_screenshot", screenshot_metas=metas,
        **{f"frames{n}": [p["path"] for p in meta["slices"]] for n, meta in enumerate(metas, 1)})
    system, user, sent = client.calls[0]
    for rule in ["不是流式过程", "主要测评证据", "找到 26 篇资料", "来源列表未展开不能扣分", "引用支撑不足", "静态长截图无法验证点击", "response_gate=unclear"]:
        assert rule in system
    assert "早期流式帧" not in system and "关键帧按时间" not in user
    assert f"产品{count}：2张" in user
    parts = sent["content_parts"]
    images = [part for part in parts if part["type"] == "image_url"]
    expected = [Path(p["path"]).read_bytes() for meta in metas for p in meta["slices"]]
    assert [base64.b64decode(p["image_url"]["url"].split(",", 1)[1]) for p in images] == expected
    assert "user_images" not in sent
    assert [m["product_no"] for m in sent["image_metadata"]] == [n for n in range(1,count+1) for _ in range(2)]
    for index, part in enumerate(parts):
        if part["type"] == "image_url":
            assert parts[index-1]["type"] == parts[index+1]["type"] == "text"
            assert "产品" in parts[index-1]["text"] and "块结束" in parts[index+1]["text"]
    assert result["needs_human_review"] is (kind == "risky")
    assert result["needs_review"] is (kind == "risky")
    assert (ls.RISKY_REVIEW_REASON in result["review_reasons"]) is (kind == "risky")


@pytest.mark.parametrize("protocol", ["0.2-simplified", "0.3"])
async def test_legacy_video_judge_keeps_encoding_and_time_rules(monkeypatch, protocol):
    calls = []
    monkeypatch.setattr("auto_eval.judges.visual_compare_judge.encode_frame", lambda path, **kw: calls.append(str(path)) or "data:image/jpeg;base64,YQ==")
    client = FakeClient(3)
    await VisualCompareJudge(client, profile(), f"qa_competitor_compare@{protocol}").evaluate(
        question="q", frames1=["a.jpg"], frames2=["b.jpg"], frames3=["c.jpg"], product_count=3)
    assert calls == ["a.jpg", "b.jpg", "c.jpg"]
    system, user, args = client.calls[0]
    assert "最终回答长截图证据规则" not in system
    assert "关键帧按" in user and "产品3录屏" in user
    assert "content_parts" not in args


@pytest.mark.parametrize("enabled", [True, False])
async def test_bailian_request_parameters_repair_and_trace(tmp_path, monkeypatch, enabled):
    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="test", vl_high_resolution_images=enabled)
    client.model = "qwen3.5-397b-a17b"
    client.trace_path = str(tmp_path / "trace.jsonl")
    requests = []
    async def fake_create(kwargs, **extra):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")], usage=SimpleNamespace(image_tokens=123))
    client._llm_create = fake_create
    monkeypatch.setattr(client, "_sampling_kwargs", lambda: {"extra_body": {"enable_thinking": False}})
    await client.complete("system", "user", content_parts=[{"type":"text","text":"产品1"}, {"type":"image_url", "image_url":{"url":"data:image/png;base64,YWJj"}}], user_image_refs=["source.png"], image_metadata=[{"product_no":1,"part_no":1}])
    assert requests[0]["extra_body"].get("vl_high_resolution_images") is (True if enabled else None)
    assert requests[0]["extra_body"]["enable_thinking"] is False
    await client.repair_json("invalid")
    assert "extra_body" not in requests[1]
    await client.complete("s", "text only")
    assert "vl_high_resolution_images" not in requests[2].get("extra_body", {})
    trace = Path(client.trace_path).read_text(encoding="utf-8")
    assert "YWJj" not in trace and "data:image" not in trace
    record = json.loads(trace.splitlines()[0])
    assert record["image_metadata"][0]["product_no"] == 1
    assert record["llm_rounds"][0]["usage"]["image_tokens"] == 123
    assert _usage_dict({"prompt_tokens_details":{"image_tokens": 456}})["image_tokens"] == 456
    client._write_trace({"error": "echo data:image/png;base64,YWJj"})
    assert "YWJj" not in Path(client.trace_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("over_budget", [True, False])
async def test_runner_history_and_context_budget(tmp_path, monkeypatch, count, over_budget):
    item = {"id":"a", "query":"q", **{f"screenshot{n}":str(save_image(tmp_path, f"{n}.png")) for n in range(1,count+1)}}
    item.update({f"frames{n}": ["stale-frame.jpg"] for n in range(1, count+1)})
    cfg = profile(max_input_tokens=1 if over_budget else 260096)
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    real_prepare = video_prepare.prepare_session_long_screenshot_item
    def prepare(item, **kwargs):
        return real_prepare(item, **kwargs, runs_dir=tmp_path / "runs")
    monkeypatch.setattr(runner, "prepare_session_long_screenshot_item", prepare)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", lambda *a, **k: pytest.fail("不应探测视频"))
    monkeypatch.setattr(runner, "_persist_task", lambda *a: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **k: None)
    client = FakeClient(count)
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    app_cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content":cfg})
    task = Task(id="test", mode="compare", items=[item], options={})
    one, _ = runner._make_item_evaluator(task, app_cfg)
    result = await one(0, item)
    summary = runner._summarize(task)
    if over_budget:
        assert result["error_type"] == "context_budget_exceeded"
        assert result["needs_human_review"] and result["needs_review"]
        assert not client.calls
        assert summary["failed"] == 1 and summary["comparable"] == 0
        assert summary["needs_human_review_count"] == 1
    else:
        assert "error" not in result
        assert summary["done"] == 1 and summary["comparable"] == 1
        assert len(client.calls) == 1
    snapshot = history.task_to_snapshot(task)
    restored = _task_from_snapshot(snapshot, "test")
    assert restored.items[0]["evidence_mode"] == "long_screenshot"
    assert len(history._item_visual_streams(restored.items[0])) == count
    assert "base64" not in json.dumps(snapshot)
    for n in range(1,count+1):
        assert not Path(item[f"screenshot_meta{n}"]["original_path"]).is_absolute()
    target = history.write_frames_zip(snapshot, tmp_path / "evidence.zip")
    with zipfile.ZipFile(target) as archive:
        assert sum(name.endswith("screenshot.json") for name in archive.namelist()) == count
        assert all(json.loads(line)["status"] == "ok" for line in archive.read("manifest.jsonl").decode().splitlines())
    rows = history.export_rows(snapshot)
    assert "视觉证据清单" in rows
    assert "识别是否需要人工复查" not in rows["逐题结果"][0]  # compare 使用其自身正式列


def test_historical_frames_without_evidence_mode_unchanged():
    item = {"video1":"a.mp4", "video2":"b.mp4", "frames1":["a.jpg"], "frames2":["b.jpg"]}
    streams = history._item_visual_streams(item)
    assert len(streams) == 2 and all(s["evidence_mode"] == "video_frames" for s in streams)
    assert runner._compare_frames_ready(item)
    items, errors = parse_jsonl(json.dumps({"query":"q", "video1":"a.mp4", "video2":"b.mp4"}), "compare")
    assert not errors and items[0]["evidence_mode"] == "video_frames"


def test_formal_export_columns_same_with_screenshot_metadata():
    original = {"index":0, "query":"q", "product_count":2}
    screenshot = {**original, "evidence_mode":"long_screenshot", "screenshot_meta1":{"split_status":"risky"}}
    assert list(history._visual_compare_export_rows([original])[0]) == list(history._visual_compare_export_rows([screenshot])[0])


def test_context_budget_counts_all_products_and_text():
    metas = [{"estimated_image_tokens":1000,"slices":[{}]}] * 3
    with pytest.raises(ls.ContextBudgetExceeded):
        ls.check_context_budget("s", ["q"], metas, LongScreenshotConfig(max_input_tokens=5000, output_reserve_tokens=2000))
    with pytest.raises(ls.ContextBudgetExceeded):
        ls.check_context_budget("s", ["很长的回答"*100], metas[:1], LongScreenshotConfig(max_input_tokens=2000, output_reserve_tokens=100))


def test_original_file_changed_after_preparation_is_rejected(tmp_path):
    path = save_image(tmp_path)
    cfg = LongScreenshotConfig()
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    Image.new("RGB", (64,180), "red").save(path)
    with pytest.raises(ValueError, match="发生变化"):
        ls.encode_original_image(path, cfg, meta["slices"][0]["sha256"])


def test_cmyk_original_keeps_bytes_and_channels(tmp_path, monkeypatch):
    path = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (64, 180), (10, 20, 30, 40)).save(path)
    original = path.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("未超限 CMYK 不得转色、缩放或重新编码")
    monkeypatch.setattr(Image.Image, "convert", forbidden)
    monkeypatch.setattr(Image.Image, "resize", forbidden)
    monkeypatch.setattr(Image.Image, "save", forbidden)
    cfg = LongScreenshotConfig()
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    url = ls.encode_original_image(path, cfg, meta["original_sha256"])
    assert meta["split_status"] == "original"
    assert base64.b64decode(url.split(",", 1)[1]) == original
    with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
        assert image.mode == "CMYK"
    assert not (tmp_path / "parts").exists()


@pytest.mark.parametrize("limit_type", ["pixels", "base64"])
async def test_oversized_cmyk_is_input_error_without_model_call(tmp_path, monkeypatch, limit_type):
    path = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (64, 180), (10, 20, 30, 40)).save(path)
    original = path.read_bytes()
    limits = {"max_pixels": 64 * 95} if limit_type == "pixels" else {
        "max_data_url_bytes": ls.data_url_size(len(original), "image/jpeg")}
    cfg = profile(**limits)
    def forbidden(*args, **kwargs):
        pytest.fail("超限 CMYK 应直接报输入错误，不进入转换或切片")
    monkeypatch.setattr(Image.Image, "convert", forbidden)
    monkeypatch.setattr(Image.Image, "save", forbidden)
    monkeypatch.setattr(ls, "_png_bytes", forbidden)
    with pytest.raises(ValueError, match="超限 CMYK JPEG"):
        ls.prepare_long_screenshot(path, tmp_path / "parts", cfg.long_screenshot)
    assert path.read_bytes() == original
    assert not (tmp_path / "parts").exists()
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    monkeypatch.setattr(runner, "_persist_task", lambda *a: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **k: None)
    client = FakeClient()
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    item = {"id": "cmyk", "query": "q", "screenshot1": str(path), "screenshot2": str(path)}
    task = Task(id="cmyk", mode="compare", items=[item], options={})
    one, _ = runner._make_item_evaluator(task, AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": cfg}))
    result = await one(0, item)
    assert "超限 CMYK JPEG" in result["error"]
    assert not client.calls
    assert not any(key.endswith("_score") or key.endswith("_gate") for key in result)


@pytest.mark.parametrize("protocol", ["0.2-simplified", "0.3"])
async def test_original_and_three_part_products_are_exclusive(tmp_path, protocol):
    cfg = profile(max_pixels=64*65)
    first = save_image(tmp_path, "first.png", size=(64, 50))
    second = save_image(tmp_path, "second.png", size=(64, 180))
    metas = [ls.prepare_long_screenshot(path, tmp_path / str(n), cfg.long_screenshot) for n,path in enumerate([first,second])]
    assert [m["split_count"] for m in metas] == [1,3]
    client = FakeClient()
    await VisualCompareJudge(client, cfg, f"qa_competitor_compare@{protocol}").evaluate(
        question="q", evidence_mode="long_screenshot", screenshot_metas=metas,
        frames1=[p["path"] for p in metas[0]["slices"]], frames2=[p["path"] for p in metas[1]["slices"]])
    sent = client.calls[0][2]
    assert sent["user_image_refs"][0] == str(first)
    assert str(second) not in sent["user_image_refs"]
    assert [m["position"] for m in sent["image_metadata"]] == ["whole","top","middle","bottom"]


async def test_high_resolution_reaches_streaming_sdk_only_for_images():
    captured = []
    class Stream:
        def __aiter__(self):
            return self.chunks()
        async def chunks(self):
            yield SimpleNamespace(model="test", usage=None, choices=[SimpleNamespace(
                delta=SimpleNamespace(content="{}", tool_calls=[]), finish_reason="stop")])
            yield SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2,
                prompt_tokens_details=SimpleNamespace(image_tokens=9)), choices=[])
        async def close(self):
            pass
    async def create(**kwargs):
        captured.append(kwargs)
        return Stream()
    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="test", vl_high_resolution_images=True)
    client.model = "qwen3.5-397b-a17b"
    client.trace_path = None
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    await client.complete("s", "u", user_images=["data:image/png;base64,YQ=="])
    await client.repair_json("bad")
    assert captured[0]["extra_body"] == {"vl_high_resolution_images":True}
    assert captured[0]["stream"] is True and captured[0]["stream_options"] == {"include_usage":True}
    assert "extra_body" not in captured[1]


@pytest.mark.parametrize("size,expected_parts", [((1080,20000),2), ((1080,35000),3)])
def test_default_pixel_limit_on_realistic_dimensions(tmp_path, size, expected_parts):
    # 真实像素上限，验证两块/三块均无需重叠或改变原宽度。
    path = save_image(tmp_path, size=size)
    cfg = LongScreenshotConfig()
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["original_pixels"] > cfg.max_pixels
    assert meta["split_count"] == expected_parts
    assert_reconstruct(path, meta, cfg)


def test_default_base64_limit_on_realistic_file(tmp_path):
    path = save_image(tmp_path, size=(1080,3000), kind="noise")
    cfg = LongScreenshotConfig()
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["original_pixels"] < cfg.max_pixels
    assert meta["original_data_url_bytes"] > cfg.max_data_url_bytes
    assert meta["split_count"] == 2
    assert_reconstruct(path, meta, cfg)


@pytest.mark.parametrize("mode", ["RGBA", "L", "P", "I;16"])
def test_png_color_modes_and_alpha_reconstruct(tmp_path, mode):
    path = tmp_path / "mode.png"
    if mode == "I;16":
        image = Image.fromarray(np.random.default_rng(2).integers(0,65536,(48,24),dtype=np.uint16))
    else:
        image = Image.new(mode, (24,48))
        if mode == "RGBA":
            image.putpixel((5,5), (10,20,30,40))
        elif mode == "P":
            image.putpalette([i for i in range(256) for _ in range(3)])
            image.putpixel((5,5), 128)
    image.save(path)
    cfg = LongScreenshotConfig(max_pixels=24*26)
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg)
    assert meta["split_count"] == 2
    assert_reconstruct(path, meta, cfg)


async def test_risky_still_forces_review_after_json_repair(tmp_path):
    cfg = profile(max_pixels=64*95)
    path = save_image(tmp_path, kind="risky")
    meta = ls.prepare_long_screenshot(path, tmp_path / "parts", cfg.long_screenshot)
    client = FakeClient()
    async def malformed(*args, **kwargs):
        return "invalid json"
    async def repair(*args, **kwargs):
        return json.dumps(model_data())
    client.complete = malformed
    client.repair_json = repair
    result = await VisualCompareJudge(client, cfg).evaluate(question="q", evidence_mode="long_screenshot",
        screenshot_metas=[meta,meta], frames1=[p["path"] for p in meta["slices"]], frames2=[p["path"] for p in meta["slices"]])
    assert result["needs_human_review"] is True
    assert result["review_reasons"].count(ls.RISKY_REVIEW_REASON) == 1


def test_existing_page_jsonl_import_preserves_visual_evidence():
    node = shutil.which("node")
    if not node:
        pytest.skip("requires Node.js for existing page JSONL serialization test")
    completed = subprocess.run([node, str(Path(__file__).with_name("test_long_screenshot_import.cjs"))], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "4 JSONL import/submit scenarios passed" in completed.stdout
