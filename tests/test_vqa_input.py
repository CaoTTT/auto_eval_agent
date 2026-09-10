import base64
import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from auto_eval import query_images as qi
from auto_eval.config import AppConfig, JudgeConfig, QueryImageConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.compare_protocols import DIMENSIONS, resolve_compare_protocol
from auto_eval.judges.visual_compare_judge import VisualCompareJudge
from auto_eval.long_screenshot import prepare_long_screenshot
from auto_eval.web import history, runner, server, video_prepare
from auto_eval.web.parse_input import parse_jsonl
from auto_eval.web.tasks import Task, _task_from_snapshot, merge_items_by_id


@pytest.fixture
def setup_images(tmp_path, monkeypatch):
    monkeypatch.setattr(qi, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(qi, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    paths = []
    for n, color in enumerate(("red", "green", "blue", "white")):
        path = tmp_path / f"{n}.png"
        Image.new("RGB", (64, 96), color).save(path)
        paths.append(str(path))
    return tmp_path, profile, paths


class Client:
    persona = "test"
    model = "fake-model"
    cfg = JudgeConfig(name="test")

    def __init__(self, count=2):
        self.calls = []
        self.count = count

    async def complete(self, system, user, **kwargs):
        self.calls.append((system, user, kwargs))
        data = {"product_count": self.count, **{f"{d}_applicable": False for d in DIMENSIONS}}
        for n in range(1, self.count + 1):
            data.update({f"answer{n}_input_status": "complete", f"answer{n}_response_gate": "pass", f"answer{n}_safety_gate": "pass"})
        return json.dumps(data)

    async def aclose(self):
        pass


@pytest.mark.parametrize("images", [None, "a.png", [""], ["a", "b"], [4]])
def test_invalid_input_not_silently_text(images):
    item = {"query": "q", "query_images": images, "video1": "a", "video2": "b"}
    parsed, errors = parse_jsonl(json.dumps(item), "compare")
    assert not parsed and errors
    with pytest.raises(HTTPException):
        server._validate_eval_request(server.EvalReq(mode="compare", items=[item]), AppConfig(judges=[]))


def test_mixed_normalization_and_untrusted_metadata():
    items = [{"query": "q", "video1": "a", "video2": "b"},
             {"query": "q", "query_images": ["a.png"], "screenshot1": "a", "screenshot2": "b"}]
    parsed, errors = parse_jsonl("\n".join(map(json.dumps, items)), "compare")
    assert not errors
    assert [it["input_modality"] for it in parsed] == ["text", "text_image"]
    parsed[1]["query_image_meta"] = [{"sha256": "forged"}]
    request = server.EvalReq(mode="compare", items=parsed)
    server._validate_eval_request(request, AppConfig(judges=[]))
    assert "query_image_meta" not in request.items[1]
    for override in ({"input_modality": "text", "query_images": ["a"]}, {"evaluation_profile": "bad"}):
        with pytest.raises(ValueError):
            qi.normalize_query_input({"query": "q", **override})


@pytest.mark.parametrize("standard", ["0.2-simplified", "0.3"])
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("evidence", ["video_frames", "long_screenshot"])
@pytest.mark.parametrize("with_image", [False, True])
async def test_all_16_combinations(setup_images, standard, count, evidence, with_image):
    root, profile, paths = setup_images
    prepared = qi.prepare_query_images({"query": "q", "query_images": [paths[0]] if with_image else []}, session_name="task", cfg=profile.query_images)
    args = {f"frames{n}": [paths[n]] for n in range(1, count + 1)}
    if evidence == "long_screenshot":
        args["screenshot_metas"] = [prepare_long_screenshot(Path(paths[n]), root / f"parts{n}", profile.long_screenshot) for n in range(1, count + 1)]
    client = Client(count)
    result = await VisualCompareJudge(client, profile, f"qa_competitor_compare@{standard}").evaluate(
        question="q", evidence_mode=evidence, product_count=count, query_image_meta=prepared["query_image_meta"], **args)
    system, user, sent = client.calls[0]
    assert result["standard_version"] == standard
    assert result["overall_winner"] is None
    if with_image:
        images = [p for p in sent["content_parts"] if p["type"] == "image_url"]
        assert len(images) == count + 1 == len(sent["image_metadata"]) == len(sent["user_image_refs"])
        assert base64.b64decode(images[0]["image_url"]["url"].split(",")[1]) == Path(paths[0]).read_bytes()
        assert sent["image_metadata"][0]["image_role"] == "query_image"
        assert "product_no" not in sent["image_metadata"][0]
        assert [m["product_no"] for m in sent["image_metadata"][1:]] == list(range(1, count + 1))
        assert all(m.get("sha256") for m in sent["image_metadata"])
        assert "input_manifest_sha256" in result
        assert "共享提问图片读取说明" in system
    else:
        assert "共享提问图片读取说明" not in system
        if evidence == "video_frames":
            assert "content_parts" not in sent


def test_snapshot_orientation_limits_and_changed_bytes(setup_images):
    root, profile, paths = setup_images
    path = root / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (40, 70), "red").save(path, exif=exif)
    item = {"query": "q", "query_images": [str(path)]}
    prepared = qi.prepare_query_images(item, session_name="a", cfg=profile.query_images)
    meta = prepared["query_image_meta"][0]
    assert (meta["width"], meta["height"]) == (70, 40)
    assert meta["transformation"] == "exif_transpose"
    assert meta["original_sha256"] != meta["sha256"]
    assert qi.encode_query_image(meta, profile.query_images).startswith("data:image/png;")
    Path(meta["path"]).write_bytes(b"bad")
    with pytest.raises(qi.QueryImageError):
        qi.encode_query_image(meta, profile.query_images)
    with pytest.raises(qi.QueryImageError, match="像素"):
        qi.prepare_query_images({"query": "q", "query_images": [paths[0]]}, session_name="b", cfg=QueryImageConfig(max_pixels=10))
    for bad in (root / "missing.png", root.parent / "outside.png"):
        with pytest.raises(qi.QueryImageError):
            qi.prepare_query_images({"query": "q", "query_images": [str(bad)]}, session_name="a", cfg=profile.query_images)
    animated = root / "animated.webp"
    Image.new("RGB", (40, 40), "red").save(animated, save_all=True, append_images=[Image.new("RGB", (40, 40), "blue")])
    with pytest.raises(qi.QueryImageError):
        qi.inspect_image(animated, profile.query_images)


@pytest.mark.parametrize("limit", [{"max_request_images": 2}, {"max_request_bytes": 100}, {"max_input_tokens": 1}])
async def test_total_budget_prevents_model_call(setup_images, limit):
    root, profile, paths = setup_images
    prepared = qi.prepare_query_images({"query": "q", "query_images": [paths[0]]}, session_name="a", cfg=profile.query_images)
    profile.query_images = profile.query_images.model_copy(update=limit)
    client = Client()
    with pytest.raises(qi.QueryImageError):
        await VisualCompareJudge(client, profile).evaluate(question="q", frames1=[paths[1]], frames2=[paths[2]], query_image_meta=prepared["query_image_meta"])
    assert not client.calls


async def test_input_fingerprint_and_rerun(setup_images):
    root, profile, paths = setup_images
    client = Client()
    fingerprints = []
    for image in [paths[0], paths[0], paths[3]]:
        meta = qi.prepare_query_images({"query": "q", "query_images": [image]}, session_name="a", cfg=profile.query_images)
        result = await VisualCompareJudge(client, profile).evaluate(question="q", answer1="a", frames1=[paths[1]], frames2=[paths[2]], query_image_meta=meta["query_image_meta"])
        fingerprints.append(result["input_manifest_sha256"])
    assert fingerprints[0] == fingerprints[1] != fingerprints[2]
    assert len(client.calls) == 3


async def test_mixed_runner_failure_cached_frames_recovery_and_exports(setup_images, monkeypatch):
    root, profile, paths = setup_images
    client = Client()
    monkeypatch.setattr(runner, "JudgeClient", lambda *a: client)
    monkeypatch.setattr(runner, "_persist_task", lambda *a: None)
    monkeypatch.setattr(runner, "_write_eval_error", lambda *a, **k: None)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", lambda *a, **k: pytest.fail("已缓存回答帧不能重抽"))
    real_prepare = video_prepare.prepare_session_long_screenshot_item
    monkeypatch.setattr(runner, "prepare_session_long_screenshot_item", lambda item, **kw: real_prepare(item, **kw, runs_dir=root / "answers"))
    items = []
    for index, (with_image, evidence) in enumerate([(False, "video_frames"), (True, "video_frames"), (False, "long_screenshot"), (True, "long_screenshot"), (True, "video_frames")]):
        item = {"id": str(index), "query": "q", "evidence_mode": evidence,
                "query_images": [paths[0] if index != 4 else str(root / "missing.png")] if with_image else [],
                "frames1": [paths[1]], "frames2": [paths[2]]}
        if evidence == "long_screenshot":
            item.update(screenshot1=paths[1], screenshot2=paths[2])
        items.append(item)
    cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile})
    manifest = server._protocol_manifest(resolve_compare_protocol(None), cfg, {})
    task = Task(id="mixed", mode="compare", items=items, options={}, session_name="mixed", protocol_manifest=manifest)
    one, _ = runner._make_item_evaluator(task, cfg)
    for index, item in enumerate(items):
        await one(index, item)
    assert len(client.calls) == 4
    assert task.results[-1]["error_type"] == "query_image_missing"
    assert task.results[-1]["input_modality"] == "text_image"
    assert "answer1_accuracy_score" not in task.results[-1]
    summary = runner._summarize(task)
    assert summary["input_modality_counts"] == {"text": 2, "text_image": 3}
    assert summary["failed"] == 1 and summary["by_input_modality"]["text_image"]["failed"] == 1
    assert not summary["by_input_modality"]["text_image"]["accuracy_aggregation_enabled"]
    snapshot = history.task_to_snapshot(task)
    restored = _task_from_snapshot(snapshot, task.id)
    assert restored.items[1]["query_image_meta"] == items[1]["query_image_meta"]
    assert items[1]["input_manifest_sha256"] == task.results[1]["input_manifest_sha256"]
    sheets = history.export_rows(snapshot)
    assert len(sheets["提问图片清单"]) == 3
    xlsx = history.build_xlsx(snapshot)
    with zipfile.ZipFile(io.BytesIO(xlsx)) as archive:
        assert "提问图片" in archive.read("xl/workbook.xml").decode()
        original_hash = items[1]["query_image_meta"][0]["original_sha256"]
        assert original_hash in {hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist() if n.startswith("xl/media/")}
    target = history.write_frames_zip(snapshot, root / "evidence.zip")
    with zipfile.ZipFile(target) as archive:
        records = [json.loads(line) for line in archive.read("manifest.jsonl").decode().splitlines()]
        qrows = [r for r in records if r.get("image_role") == "query_image"]
        assert len(qrows) == 6
        assert sum(r["status"] == "ok" for r in qrows) == 4
    # Explicit removal clears previous image metadata and scores immediately.
    merge_items_by_id(task, [{"id": "1", "query": "q", "query_images": [], "video1": "a", "video2": "b"}])
    assert "query_image_meta" not in task.items[1]
    assert not any(r["index"] == 1 for r in task.results)


async def test_upload_and_registered_preview(setup_images, monkeypatch):
    root, profile, paths = setup_images
    monkeypatch.setattr(server, "cfg", lambda: AppConfig(judges=[], visual_modes={"rich_content": profile}))
    upload = UploadFile(filename="question.png", file=io.BytesIO(Path(paths[0]).read_bytes()))
    result = await server.api_upload_query_image(upload)
    response = server.api_query_image(result["image_id"])
    assert str(response.path) == result["path"]
    for key in ("../secret", "0" * 32):
        with pytest.raises(HTTPException):
            server.api_query_image(key)


async def test_replace_requires_explicit_images_and_legacy_rejected(setup_images, monkeypatch):
    root, profile, paths = setup_images
    task = Task(id="old", mode="compare", items=[{"id": "a", "query": "q", "query_images": [paths[0]]}], options={}, status="done")
    monkeypatch.setattr(server, "get_task", lambda _: task)
    monkeypatch.setattr(server, "cfg", lambda: AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile}))
    with pytest.raises(HTTPException, match="显式"):
        await server.api_eval_items(server.EvalItemsReq(task_id="old", items=[{"id": "a", "query": "q", "video1": "a", "video2": "b"}]))
    with pytest.raises(HTTPException, match="旧任务"):
        await server.api_eval_items(server.EvalItemsReq(task_id="old", items=[{"id": "a", "query": "q", "query_images": [paths[0]], "video1": "a", "video2": "b"}]))


def test_subsets_use_actual_scores_and_denominators():
    items = [{"query": "q", "query_images": []}] + [{"query": "q", "query_images": ["a"]} for _ in range(3)]
    results = [{"index": i, "understanding_applicable": True, "answer1_understanding_score": score,
                "answer2_understanding_score": score, "product_count": 2} for i, score in enumerate([1, 5, 5, 5])]
    summary = runner._summarize(Task(id="s", mode="compare", items=items, results=results, options={}))
    assert summary["understanding_answer1_avg"] == 4
    assert summary["by_input_modality"]["text"]["understanding_answer1_avg"] == 1
    assert summary["by_input_modality"]["text_image"]["understanding_answer1_avg"] == 5


async def test_real_client_http_parts_and_trace_are_aligned(setup_images, monkeypatch):
    from auto_eval.judges.base import JudgeClient
    root, profile, paths = setup_images
    prepared = qi.prepare_query_images({"query": "q", "query_images": [paths[0]]}, session_name="trace", cfg=profile.query_images)
    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="test")
    client.model = "fake-model"
    client.persona = "test"
    client.trace_path = str(root / "trace.jsonl")
    requests = []
    fake = Client()
    async def create(kwargs, **extra):
        requests.append(kwargs)
        content = await fake.complete("", "")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")], usage=None)
    client._llm_create = create
    monkeypatch.setattr(client, "_sampling_kwargs", lambda: {})
    await VisualCompareJudge(client, profile).evaluate(question="q", frames1=[paths[1]], frames2=[paths[2]], query_image_meta=prepared["query_image_meta"])
    parts = requests[0]["messages"][1]["content"]
    assert len([p for p in parts if p["type"] == "image_url"]) == 3
    assert parts[1]["text"].startswith("提问图片 QI1")
    trace = Path(client.trace_path).read_text(encoding="utf-8")
    assert "data:image" not in trace
    record = json.loads(trace.splitlines()[0])
    assert record["image_metadata"][0]["image_role"] == "query_image"
    assert record["image_metadata"][1]["product_no"] == 1


def test_prepared_snapshot_is_stable_and_preserves_source(setup_images):
    root, profile, paths = setup_images
    prepared = qi.prepare_query_images({"query": "q", "query_images": [paths[0]]}, session_name="stable", cfg=profile.query_images)
    Image.new("RGB", (64, 96), "black").save(paths[0])
    again = qi.prepare_query_images(prepared, session_name="stable", cfg=profile.query_images)
    assert again["query_image_meta"][0]["source_path"] == paths[0]
    assert again["query_image_meta"][0]["image_id"] == prepared["query_image_meta"][0]["image_id"]
    assert again["query_image_meta"][0]["sha256"] == prepared["query_image_meta"][0]["sha256"]
    changed = qi.prepare_query_images({"query": "q", "query_images": [paths[0]]}, session_name="stable", cfg=profile.query_images)
    assert changed["query_image_meta"][0]["sha256"] != again["query_image_meta"][0]["sha256"]


async def test_cancelled_preparation_does_not_publish_images(setup_images, monkeypatch):
    import threading
    from auto_eval.preparation import run_preparation
    root, profile, paths = setup_images
    started, release = threading.Event(), threading.Event()
    original_inspect = qi.inspect_image
    def paused(path, cfg):
        result = original_inspect(path, cfg)
        started.set()
        release.wait(5)
        return result
    monkeypatch.setattr(qi, "inspect_image", paused)
    work = asyncio.create_task(run_preparation(qi.prepare_query_images, {"query": "q", "query_images": [paths[0]]}, session_name="cancel", cfg=profile.query_images, timeout=30))
    assert await asyncio.to_thread(started.wait, 5)
    work.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert not (root / "runs/query_images/registry").exists()


async def test_api_replacement_uses_sanitized_normalized_item(setup_images, monkeypatch):
    root, profile, paths = setup_images
    cfg = AppConfig(judges=[JudgeConfig(name="test")], visual_modes={"rich_content": profile})
    task = Task(id="existing", mode="compare", items=[{"id": "a", "query": "q", "query_images": [paths[0]]}], options={}, status="done",
                protocol_manifest=server._protocol_manifest(resolve_compare_protocol(None), cfg, {}))
    monkeypatch.setattr(server, "cfg", lambda: cfg)
    monkeypatch.setattr(server, "get_task", lambda _: task)
    monkeypatch.setattr(server, "queue_task_save", lambda *a, **k: None)
    monkeypatch.setattr(server, "spawn_background", lambda coro: coro.close())
    await server.api_eval_items(server.EvalItemsReq(task_id="existing", items=[{
        "id": "a", "question": "new", "query_images": [], "video1": "a", "video2": "b",
        "query_image_meta": [{"sha256": "fake"}], "input_manifest_sha256": "fake",
    }]))
    assert task.items[0]["query"] == "new"
    assert task.items[0]["input_modality"] == "text"
    assert "query_image_meta" not in task.items[0]
    assert "input_manifest_sha256" not in task.items[0]


async def test_frozen_model_policy_and_legacy_revision(setup_images, monkeypatch):
    root, profile, paths = setup_images
    cfg = AppConfig(judges=[JudgeConfig(name="test", model="original")], visual_modes={"rich_content": profile})
    manifest = server._protocol_manifest(resolve_compare_protocol(None), cfg, {})
    cfg.judges[0].model = "changed"
    profile.query_images.max_request_images = 1
    captured = {}
    def client(configuration):
        captured["model"] = configuration.model
        return Client()
    def judge(client, frozen_profile, protocol):
        captured["limit"] = frozen_profile.query_images.max_request_images
        return object()
    monkeypatch.setattr(runner, "JudgeClient", client)
    monkeypatch.setattr(runner, "VisualCompareJudge", judge)
    runner._make_item_evaluator(Task(id="a", mode="compare", items=[], options={}, protocol_manifest=manifest), cfg)
    assert captured == {"model": "original", "limit": 64}
    legacy = resolve_compare_protocol(None, "0.2.0")
    assert legacy.public_metadata()["input_modalities"] == ["text"]
    with pytest.raises(ValueError):
        resolve_compare_protocol(None, "unavailable")


def test_frontend_query_image_roundtrip():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend regression")
    completed = subprocess.run([node, str(Path(__file__).with_name("test_vqa_ui.cjs"))], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize("count", [2, 3])
async def test_screenshot_preview_and_download_return_original_bytes(setup_images, monkeypatch, count):
    import httpx
    root, profile, paths = setup_images
    item = {"id": "case", "query": "q", "evidence_mode": "long_screenshot", "product_count": count}
    for n in range(1, count + 1):
        item[f"screenshot{n}"] = paths[n]
        item[f"screenshot_meta{n}"] = {"original_path": paths[n], "original_sha256": hashlib.sha256(Path(paths[n]).read_bytes()).hexdigest()}
    task = Task(id="images", mode="compare", items=[item], options={})
    monkeypatch.setattr(server, "peek_task", lambda tid, **kw: task if tid == task.id else None)
    monkeypatch.setattr(server, "load_snapshot", lambda _: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        for n in range(1, count + 1):
            url = f"/api/eval/images/items/0/screenshots/{n}"
            preview = await client.get(url)
            download = await client.get(url + "?download=true")
            assert preview.status_code == download.status_code == 200
            assert preview.content == download.content == Path(paths[n]).read_bytes()
            assert preview.headers["content-type"] == "image/png"
            assert "content-disposition" not in preview.headers
            assert f"product{n}_original" in download.headers["content-disposition"]
            assert download.headers["content-disposition"].startswith("attachment;")
        for url in ("/api/eval/missing/items/0/screenshots/1", "/api/eval/images/items/9/screenshots/1", "/api/eval/images/items/0/screenshots/4"):
            assert (await client.get(url)).status_code == 404
        Image.new("RGB", (64, 96), "black").save(paths[1])
        assert (await client.get("/api/eval/images/items/0/screenshots/1")).status_code == 409
        monkeypatch.setattr(server, "operation_video_roots", lambda _: [root / "restricted"])
        assert (await client.get("/api/eval/images/items/0/screenshots/2")).status_code == 404


async def test_query_image_download_preserves_original(setup_images):
    import httpx
    root, profile, paths = setup_images
    prepared = qi.prepare_query_images({"query": "q", "query_images": [paths[0]]}, session_name="download", cfg=profile.query_images)
    meta = prepared["query_image_meta"][0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test") as client:
        response = await client.get(meta["preview_url"] + "?original=true")
    assert response.status_code == 200
    assert response.content == Path(paths[0]).read_bytes()
    assert response.headers["content-disposition"].startswith("attachment;")
