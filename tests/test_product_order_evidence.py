"""Screenshot audit exports distinguish source products from judge positions."""
import base64
from copy import deepcopy
import hashlib
import json
import zipfile

from PIL import Image
import pytest

from auto_eval import evidence_audit
from auto_eval.product_order import product_order_metadata
from auto_eval.web.screenshot_evidence import evidence_records, evidence_sheets, write_evidence_zip


@pytest.fixture
def source_images(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_audit, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    paths = []
    for number, color in enumerate(("red", "blue"), 1):
        path = tmp_path / f"original_product{number}.png"
        Image.new("RGB", (8, 8), color).save(path)
        paths.append(path)
    return paths


def recorded_snapshot(paths, reverse):
    parts = [{"type": "text", "text": "产品1 / 产品2：保持实际裁判请求编号"}]
    images, preprocessing = [], []
    order = [2, 1] if reverse else [1, 2]
    for slot, original in enumerate(order, 1):
        path = paths[original - 1]
        raw = path.read_bytes()
        parts.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
        }})
        images.append({"image_role": "product_answer", "product_no": slot, "ref_path": str(path)})
        preprocessing.append({"product_no": slot, "metadata": {
            "original_path": str(path), "original_sha256": hashlib.sha256(raw).hexdigest(),
            "split_status": "original", "split_count": 1,
        }})
    record = evidence_audit.capture_request("原始system产品1和产品2", parts, images, preprocessing, {})
    record.update(product_order_metadata(reverse), record_status="model_response_received")
    return {"mode": "compare", "options": {"reverse_product_order": reverse}, "items": [{
        "id": "order-case", "product_count": 2, "evidence_mode": "long_screenshot",
        "screenshot1": str(paths[0]), "screenshot2": str(paths[1]), "screenshot_evidence": record,
    }]}


@pytest.mark.parametrize("reverse", [False, True])
def test_evidence_sheets_keep_original_product_columns_and_add_judge_position(source_images, reverse):
    snapshot = recorded_snapshot(source_images, reverse)
    before = deepcopy(snapshot)
    expected = [2, 1] if reverse else [1, 2]
    sheets = evidence_sheets(snapshot)
    for name in ("长截图预处理参数", "长截图请求图片序列"):
        rows = sheets[name]
        assert [row["产品序号"] for row in rows] == expected
        assert [row["裁判位置"] for row in rows] == [1, 2]
    for rows in sheets.values():
        for row in rows:
            assert row["裁判产品位置互换"] == ("是" if reverse else "否")
            assert row["裁判产品呈现顺序"] == " → ".join(f"原产品{n}" for n in expected)
            assert f"裁判位置1=原产品{expected[0]}" in row["裁判产品位置说明"]
    image_rows = sheets["长截图请求图片序列"]
    assert [row["图片路径"] for row in image_rows] == [str(source_images[n - 1]) for n in expected]
    for row, original in zip(image_rows, expected, strict=True):
        assert row["SHA256"] == hashlib.sha256(source_images[original - 1].read_bytes()).hexdigest()
    system = [row["完整内容"] for row in sheets["长截图Prompt明细"] if row["请求位置"] == "system"]
    assert system == [before["items"][0]["screenshot_evidence"]["system_prompt"]]
    assert snapshot == before


@pytest.mark.parametrize("reverse", [False, True])
def test_evidence_zip_manifest_keeps_judge_slots_and_identifies_original_products(source_images, tmp_path, reverse):
    snapshot = recorded_snapshot(source_images, reverse)
    before = deepcopy(snapshot)
    expected = [2, 1] if reverse else [1, 2]
    with zipfile.ZipFile(write_evidence_zip(snapshot, tmp_path / "order-evidence.zip")) as archive:
        record = json.loads(archive.read("manifest.json"))["records"][0]
        assert record["judge_product_order"] == expected
        assert record["reverse_product_order"] is reverse
        for entries in (record["images"], record["preprocessing"]):
            assert [entry["product_no"] for entry in entries] == [1, 2]
            assert [entry["original_product_no"] for entry in entries] == expected
        for entry, original in zip(record["images"], expected, strict=True):
            raw = source_images[original - 1].read_bytes()
            assert entry["ref_path"] == str(source_images[original - 1])
            assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
            assert archive.read(entry["archive_path"]) == raw
        assert record["request_sha256"] == before["items"][0]["screenshot_evidence"]["request_sha256"]
        assert record["system_prompt"] == before["items"][0]["screenshot_evidence"]["system_prompt"]
        assert record["content_sequence"] == before["items"][0]["screenshot_evidence"]["content_sequence"]
        assert "original_product_no" in archive.read("README.txt").decode("utf-8")
    assert snapshot == before


def test_legacy_evidence_does_not_claim_a_recorded_judge_position(source_images):
    snapshot = recorded_snapshot(source_images, True)
    item = snapshot["items"][0]
    record = item.pop("screenshot_evidence")
    for entry in record["preprocessing"]:
        original = 3 - entry["product_no"]
        item[f"screenshot_meta{original}"] = {**entry["metadata"], "slices": [{
            "path": str(source_images[original - 1]), "sha256": entry["metadata"]["original_sha256"],
        }]}
    legacy = evidence_records(snapshot)[0]
    assert legacy["record_status"] == "request_not_recorded"
    assert legacy["system_prompt"] == "" and legacy["content_sequence"] == []
    assert [image["original_product_no"] for image in legacy["images"]] == [1, 2]
    for name in ("长截图预处理参数", "长截图请求图片序列"):
        rows = evidence_sheets(snapshot)[name]
        assert [row["产品序号"] for row in rows] == [1, 2]
        assert all(row["裁判位置"] is None for row in rows)
    item["screenshot_evidence"] = deepcopy(legacy)
    for entry in item["screenshot_evidence"]["images"]:
        entry.pop("original_product_no", None)
    assert [image["original_product_no"] for image in evidence_records(snapshot)[0]["images"]] == [1, 2]
