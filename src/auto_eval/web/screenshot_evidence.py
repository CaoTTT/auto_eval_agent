"""Shared screenshot audit view for task UI, Excel and evidence ZIP exports."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import zipfile

from PIL import Image

from ..paths import PROJECT_ROOT, RUNS_DIR
from .video_prepare import operation_video_roots


def evidence_records(snapshot: dict, item_indexes: set[int] | None = None) -> list[dict]:
    records = []
    for index, item in enumerate(snapshot.get("items") or []):
        if item_indexes is not None and index not in item_indexes:
            continue
        if item.get("evidence_mode") == "video_frames":
            continue
        if not (item.get("screenshot_evidence") or item.get("screenshot1") or item.get("screenshot_meta1")):
            continue
        record = deepcopy(item.get("screenshot_evidence") or {})
        if not record:
            record = {"version": "legacy", "record_status": "request_not_recorded",
                      "system_prompt": "", "content_sequence": [], "images": [], "preprocessing": [],
                      "notice": "仅展示已保存的预处理结果；实际 Prompt 和完整历史图片顺序未记录。"}
            count = item.get("product_count") or (3 if item.get("screenshot3") or item.get("screenshot_meta3") else 2)
            for n in range(1, count + 1):
                meta = item.get(f"screenshot_meta{n}") or {}
                record["preprocessing"].append({"product_no": n, "source_turn": item.get("turn_index"), "metadata": meta})
                for i, part in enumerate(meta.get("slices") or [], 1):
                    record["images"].append({**part, "ref_path": part["path"],
                        "image_no": len(record["images"]) + 1, "image_role": "product_answer",
                        "product_no": n, "part_no": i, "part_count": len(meta["slices"]),
                        "source_turn": item.get("turn_index"), "split_status": meta.get("split_status")})
        record.update(item_index=index, item_id=item.get("id", str(index + 1)),
                      session_id=item.get("session_id"), target_turn=item.get("turn_index"),
                      request_budget_report=item.get("request_budget_report"),
                      image_findings=item.get("image_findings", []))
        records.append(record)
    return records


def evidence_bytes(image: dict) -> tuple[bytes, str]:
    raw_path = image.get("evidence_path") or image.get("ref_path") or image.get("path")
    if not raw_path:
        raise ValueError("missing: 未保存图片路径")
    path = Path(raw_path).expanduser()
    path = (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
    roots = [*operation_video_roots(PROJECT_ROOT), RUNS_DIR.resolve()]
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError("unauthorized: 图片不在允许目录中")
    if not image.get("sha256"):
        raise ValueError("unverified: 旧记录未保存图片哈希，无法确认评测证据")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError("missing: 证据文件不可读取") from exc
    if hashlib.sha256(raw).hexdigest() != image["sha256"]:
        raise ValueError("changed: 图片已变化，与本次评测证据不一致")
    with Image.open(io.BytesIO(raw)) as im:
        if im.format not in {"PNG", "JPEG", "WEBP"} or getattr(im, "n_frames", 1) != 1:
            raise ValueError("unsupported: 证据不是支持的静态图片")
        mime = Image.MIME[im.format]
    return raw, mime


def write_evidence_zip(snapshot: dict, destination: Path, item_indexes: set[int] | None = None) -> Path:
    records = evidence_records(snapshot, item_indexes)
    if not records:
        raise ValueError("任务没有长截图证据")
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in records:
                for image in record["images"]:
                    try:
                        raw, mime = evidence_bytes(image)
                    except (ValueError, OSError) as exc:
                        image.update(archive_status="unavailable", archive_error=str(exc), archive_path="")
                        continue
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
                    name = f"case_{record['item_index'] + 1:04d}/image_{image['image_no']:04d}{suffix}"
                    archive.writestr(name, raw)
                    image.update(archive_status="ok", archive_path=name)
            archive.writestr("manifest.json", json.dumps({"version": "screenshot-evidence-1", "records": records}, ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "图片按 case/image 编号对应 manifest.json 中的请求顺序。无像素拼接。\n"
                "未切分使用原图；切分使用切片；多轮请求包含历史证据及已记录题图。\n"
                "缺失、变化或无法校验的图片不会混入证据包，详见 archive_status/archive_error。\n"
                "旧任务 request_not_recorded 仅含已保存的预处理证据，不能视为完整请求回放。\n")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _chunks(value) -> list[str]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    # Stay below Excel's 32767 UTF-16-unit cell limit, including non-BMP text.
    return [text[i:i + 14000] for i in range(0, len(text), 14000)] or [""]


def evidence_sheets(snapshot: dict) -> dict[str, list[dict]]:
    parameters, images, prompts = [], [], []
    for record in evidence_records(snapshot):
        base = {"数据集序号": record["item_index"] + 1, "id": record["item_id"],
                "会话ID": record.get("session_id"), "目标轮次": record.get("target_turn"),
                "记录状态": record["record_status"]}
        for entry in record["preprocessing"]:
            meta = entry["metadata"]
            for chunk_no, chunk in enumerate(_chunks(meta), 1):
                parameters.append({**base, "产品序号": entry["product_no"], "来源轮次": entry.get("source_turn"),
                    "是否切分": meta.get("was_split", meta.get("split_status") != "original" if meta else "未记录"),
                    "切分状态": meta.get("split_status", "未准备"), "分片数量": meta.get("split_count"),
                    "原图宽": meta.get("original_width"), "原图高": meta.get("original_height"),
                    "原图SHA256": meta.get("original_sha256"), "算法版本": meta.get("algorithm_version"),
                    "参数JSON分段": chunk_no, "完整预处理参数JSON": chunk})
        for image in record["images"]:
            for chunk_no, chunk in enumerate(_chunks(image), 1):
                images.append({**base, "请求图片序号": image["image_no"], "图片角色": image.get("image_role", "product_answer"),
                    "产品序号": image.get("product_no"), "来源轮次": image.get("source_turn"),
                    "历史或当前": image.get("request_role"), "图片路径": image.get("ref_path"),
                    "SHA256": image.get("sha256"), "宽": image.get("width"), "高": image.get("height"),
                    "起始行": image.get("start_y"), "结束行": image.get("end_y"),
                    "参数JSON分段": chunk_no, "完整图片参数JSON": chunk})
        values = [("request_metadata", {k: v for k, v in record.items() if k not in {"images", "preprocessing", "system_prompt", "content_sequence"}}),
                  ("system", record["system_prompt"])] + [(f"user_part_{i}", part) for i, part in enumerate(record["content_sequence"], 1)]
        for label, value in values:
            for chunk_no, chunk in enumerate(_chunks(value), 1):
                prompts.append({**base, "请求位置": label, "分段序号": chunk_no, "完整内容": chunk})
    return {name: rows for name, rows in {"长截图预处理参数": parameters, "长截图请求图片序列": images,
                                        "长截图Prompt明细": prompts}.items() if rows}
