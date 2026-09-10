"""Shared question images: validation, immutable snapshots and request budgets."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
import warnings
from pathlib import Path

from PIL import Image, ImageOps

from .config import QueryImageConfig
from .paths import PROJECT_ROOT, RUNS_DIR, resolve_project_path
from .preparation import check_preparation

INPUT_SCHEMA_VERSION = "1.1"
PREPROCESS_VERSION = "query-image-1"
PREPARED_FIELDS = {"query_image_meta", "query_image_views", "input_manifest_sha256",
                   "input_schema_version", "query_image_preprocess_version"}
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


class QueryImageError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def normalize_query_input(item: dict) -> dict:
    query = item.get("query") or item.get("question")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query/question 必须是非空文字")
    if any(key in item for key in ("evaluation_profile", "standard_id", "standard_version", "bundle_revision")):
        raise ValueError("请在任务级选择标准，不能逐题覆盖评测标准")
    images = item.get("query_images", [])
    if not isinstance(images, list) or len(images) > 1:
        raise ValueError("query_images 必须是数组，首版允许 0 或 1 张静态图片")
    if any(not isinstance(path, str) or not path.strip() for path in images):
        raise ValueError("query_images 中的路径必须是非空字符串")
    modality = "text_image" if images else "text"
    if "input_modality" in item and item["input_modality"] != modality:
        raise QueryImageError("input_modality_mismatch", "input_modality 与提问图片声明冲突")
    return {"query": query.strip(), "query_images": [p.strip() for p in images], "input_modality": modality}


def authorized_path(raw: str, cfg: QueryImageConfig) -> Path:
    if "://" in raw:
        raise QueryImageError("query_image_invalid", "提问图片仅支持授权本地文件")
    path = resolve_project_path(raw).resolve()
    roots = [PROJECT_ROOT.resolve(), RUNS_DIR.resolve(), *[resolve_project_path(p).resolve() for p in cfg.allowed_roots]]
    if not any(path.is_relative_to(root) for root in roots):
        raise QueryImageError("query_image_invalid", "提问图片不在授权目录中")
    return path


def inspect_image(path: Path, cfg: QueryImageConfig) -> tuple[bytes, bytes, dict]:
    check_preparation()
    if not path.is_file():
        raise QueryImageError("query_image_missing", "提问图片文件不存在")
    if path.stat().st_size > cfg.max_file_bytes:
        raise QueryImageError("query_image_limit_exceeded", "提问图片文件大小超限")
    raw = path.read_bytes()
    if len(raw) > cfg.max_file_bytes:
        raise QueryImageError("query_image_limit_exceeded", "提问图片文件大小超限")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as im:
                fmt = im.format
                if FORMATS.get(path.suffix.lower()) != fmt or getattr(im, "n_frames", 1) != 1:
                    raise ValueError("仅支持格式匹配的静态 PNG/JPEG/WebP")
                if im.width * im.height > cfg.max_pixels or min(im.size) < cfg.min_edge:
                    raise QueryImageError("query_image_limit_exceeded", "提问图片像素或边长超限")
                im.load()
                orientation = im.getexif().get(274, 1)
                model = raw
                model_fmt = fmt
                size = im.size
                original_size = im.size
                if orientation not in (None, 1):
                    transformed = ImageOps.exif_transpose(im)
                    buf = io.BytesIO()
                    transformed.save(buf, format="PNG")
                    model, model_fmt, size = buf.getvalue(), "PNG", transformed.size
    except QueryImageError:
        raise
    except Exception as exc:
        raise QueryImageError("query_image_invalid", f"提问图片无法解码：{exc}") from exc
    if len(model) > cfg.max_file_bytes or 4 * ((len(model) + 2) // 3) + 32 >= cfg.max_data_url_bytes:
        raise QueryImageError("query_image_limit_exceeded", "提问图片编码后大小超限")
    check_preparation()
    return raw, model, {"original_sha256": hashlib.sha256(raw).hexdigest(),
                        "sha256": hashlib.sha256(model).hexdigest(), "format": fmt,
                        "model_format": model_fmt, "width": size[0], "height": size[1],
                        "original_width": original_size[0], "original_height": original_size[1],
                        "transformation": "exif_transpose" if model != raw else "none",
                        "preprocess_version": PREPROCESS_VERSION}


def prepare_query_images(item: dict, *, session_name: str, cfg: QueryImageConfig) -> dict:
    normalized = normalize_query_input(item)
    metas = []
    # Hash the owner name instead of placing client-supplied task IDs in paths.
    owner = hashlib.sha256(session_name.encode()).hexdigest()[:24]
    root = RUNS_DIR / "query_images" / owner
    previous = item.get("query_image_meta") or []
    for index, source in enumerate(normalized["query_images"]):
        path = authorized_path(source, cfg)
        raw, model, info = inspect_image(path, cfg)
        if index < len(previous) and previous[index].get("original_path") == str(path):
            if previous[index].get("original_sha256") != info["original_sha256"]:
                raise QueryImageError("query_image_changed", "已固化的提问原图发生变化")
        folder = root / info["original_sha256"]
        folder.mkdir(parents=True, exist_ok=True)
        original = folder / ("original" + path.suffix.lower())
        view = folder / ("model.png" if model != raw else original.name)
        check_preparation()
        _snapshot_bytes(original, raw)
        if view != original:
            _snapshot_bytes(view, model)
        prior = previous[index] if index < len(previous) else {}
        same_original = prior.get("original_path") == str(path) and prior.get("original_sha256") == info["original_sha256"]
        image_id = (prior.get("image_id") if same_original else None) or uuid.uuid4().hex
        meta = {**info, "image_id": image_id, "query_image_id": f"QI{index + 1}",
                "image_role": "query_image", "source_path": prior.get("source_path", source) if same_original else source,
                "original_path": str(original), "path": str(view),
                "preview_url": f"/api/query-images/{image_id}"}
        registry = RUNS_DIR / "query_images" / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / f"{image_id}.json").write_text(json.dumps(meta), encoding="utf-8")
        metas.append(meta)
    return {**normalized, "query_images": [m["original_path"] for m in metas],
            "query_image_meta": metas, "query_image_views": [m["path"] for m in metas],
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "query_image_preprocess_version": PREPROCESS_VERSION}


def encode_query_image(meta: dict, cfg: QueryImageConfig) -> str:
    path = authorized_path(meta["path"], cfg)
    raw, _, _ = inspect_image(path, cfg)
    if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise QueryImageError("query_image_changed", "模型提问图片与准备记录不一致")
    mime = Image.MIME[meta["model_format"]]
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _snapshot_bytes(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise QueryImageError("query_image_changed", "已固化图像内容发生变化")
        return
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(content)
        check_preparation()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def check_request_budget(system: str, parts: list[dict], cfg: QueryImageConfig) -> list[dict]:
    """Inspect actual encoded views; estimates are local guardrails, not billing."""
    views = []
    for part in parts:
        check_preparation()
        if part["type"] != "image_url":
            continue
        url = part["image_url"]["url"]
        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > cfg.max_pixels or len(url) >= cfg.max_data_url_bytes:
                raise QueryImageError("request_image_limit_exceeded", "请求中的单图超过模型图片限制")
            views.append({"sha256": hashlib.sha256(raw).hexdigest(), "width": im.width, "height": im.height})
    if len(views) > cfg.max_request_images:
        raise QueryImageError("request_image_limit_exceeded", "题图和回答证据的总图片数超限")
    payload_size = len(json.dumps({"system": system, "content": parts}, ensure_ascii=False).encode())
    if payload_size + cfg.request_overhead_bytes > cfg.max_request_bytes:
        raise QueryImageError("request_image_limit_exceeded", "题图和回答证据的请求体总大小超限")
    text = system + "".join(p.get("text", "") for p in parts)
    estimate = len(text.encode("utf-8")) + sum(((v["width"] + 31) // 32) * ((v["height"] + 31) // 32) + 256 for v in views)
    if estimate > cfg.max_input_tokens or estimate + cfg.output_reserve_tokens > cfg.context_window:
        raise QueryImageError("context_budget_exceeded", "图文请求的保守上下文预算超限")
    return views


def input_manifest(question: str, context: str, parts: list[dict], views: list[dict], protocol: dict, policy: dict) -> str:
    # Exclude evaluation time from identity; preserve role order and all answer text.
    payload = {"query": question, "context": context, "views": views, "protocol": protocol,
               "policy": policy, "preprocess_version": PREPROCESS_VERSION,
               "roles": [p.get("text", "") for p in parts[1:] if p["type"] == "text"]}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
