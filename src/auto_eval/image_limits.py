"""Frozen provider capabilities and durable image/request diagnostics."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from urllib.parse import urlparse

from PIL import Image

from .conversation import digest
from .long_screenshot import data_url_size, MIME_TYPES
from .query_images import QueryImageError
from .preparation import check_preparation

SOURCE = "https://help.aliyun.com/zh/model-studio/vision"
PROFILE_VERSION = "bailian-base64-2026-09-18-v1"
REQUEST_BUDGET_VERSION = "encoded-request-budget-v2"


def resolve_image_limits(judge, profile) -> dict:
    host = (urlparse(getattr(judge, "base_url", None) or "").hostname or "").lower()
    model_name = getattr(judge, "model", None)
    model = (model_name or "").lower()
    verified = (host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
                or host.endswith(".maas.aliyuncs.com")) and model.startswith(("qwen3.5-", "qwen3.6-", "qwen3.7-", "qwen3.8-", "qwen3-vl-"))
    high_resolution = bool(getattr(judge, "vl_high_resolution_images", False))
    cap = (16_777_216 if high_resolution else 2_621_440) if verified else None
    declared = {key: getattr(judge, key, None) for key in ("max_input_tokens", "context_window")}
    result = {"provider": "bailian" if verified else "unverified", "model": model_name,
              "endpoint_host": host, "api_family": getattr(judge, "runner", "openai_compat"), "transport": "base64",
              "capability_verified": bool(verified), "effective_pixel_cap": cap,
              "vl_high_resolution_images": high_resolution,
              "official_rules": {"file_bytes": 20_000_000, "data_uri_bytes": 20_000_000,
                  "min_edge": 11, "aspect_ratio": 200, "request_images": 250} if verified else {},
              "source_url": SOURCE if verified else None, "verified_at": "2026-09-18" if verified else None,
              "byte_interpretation": "20 MB; conservative decimal bytes (provider unit unspecified)",
              "local_guardrails": profile.query_images.model_dump(),
              "answer_guardrails": profile.long_screenshot.model_dump(),
              "limits_profile_version": PROFILE_VERSION if verified else "unverified-v1",
              "model_declared_limits": declared, "model_context_limits_verified": False,
              "model_token_limits_origin": "operator_declared_judge_config" if any(declared.values()) else "visual_profile_local_guardrails"}
    result["limits_profile_sha256"] = digest(result)
    return result


def effective_profile(profile, limits: dict):
    cap = limits.get("effective_pixel_cap")
    if not limits.get("capability_verified") or not cap:
        raise QueryImageError("image_limits_unverified", "官方图片限制及有效视觉阈值未核验，严格原图多轮评估已阻断")
    hard = limits["official_rules"]
    q = profile.query_images.model_copy(update={
        "max_pixels": min(profile.query_images.max_pixels, cap),
        "max_file_bytes": min(profile.query_images.max_file_bytes, hard["file_bytes"]),
        "max_data_url_bytes": min(profile.query_images.max_data_url_bytes, hard["data_uri_bytes"] + 1),
        "max_request_images": min(profile.query_images.max_request_images, hard["request_images"]),
        "min_edge": max(profile.query_images.min_edge, hard["min_edge"]),
    })
    s = profile.long_screenshot.model_copy(update={
        "max_pixels": min(profile.long_screenshot.max_pixels, cap),
        "max_data_url_bytes": min(profile.long_screenshot.max_data_url_bytes, hard["data_uri_bytes"] + 1,
                                  data_url_size(hard["file_bytes"]) + 1),
    })
    return profile.model_copy(update={"query_images": q, "long_screenshot": s})


def inspect_asset(path, identity: dict, limits: dict) -> tuple[dict, list[dict]]:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as im:
        width, height, fmt = im.width, im.height, im.format
        if fmt not in MIME_TYPES or getattr(im, "n_frames", 1) != 1:
            raise QueryImageError("image_format_invalid", "只支持静态 PNG/JPEG/WebP")
    asset = {**identity, "original_sha256": hashlib.sha256(raw).hexdigest(),
             "width": width, "height": height, "format": fmt, "file_bytes": len(raw),
             "data_uri_bytes": data_url_size(len(raw), MIME_TYPES[fmt]), "original_path": str(path)}
    observed = {"pixels": width * height, "file_bytes": len(raw), "data_uri_bytes": asset["data_uri_bytes"],
                "min_edge": min(width, height), "aspect_ratio": max(width, height) / min(width, height)}
    rules = [(key, value, "official_hard", ">=" if key == "min_edge" else "<=")
             for key, value in limits["official_rules"].items() if key in observed]
    if limits.get("effective_pixel_cap"):
        rules.append(("pixels", limits["effective_pixel_cap"], "effective_vision", "<="))
    local = limits["local_guardrails"] if identity["image_role"] == "query_image" else limits["answer_guardrails"]
    for key, config_key in (("pixels", "max_pixels"), ("file_bytes", "max_file_bytes"), ("data_uri_bytes", "max_data_url_bytes"),
                            ("min_edge", "min_edge"), ("aspect_ratio", "max_aspect_ratio")):
        if config_key in local:
            rules.append((key, local[config_key], "local_guardrail", ">=" if key == "min_edge" else "<" if key == "data_uri_bytes" else "<="))
    findings = []
    for metric, limit, kind, comparator in rules:
        value = observed[metric]
        passed = value >= limit if comparator == ">=" else value < limit if comparator == "<" else value <= limit
        if passed:
            continue
        finding = {**identity, "code": f"image_{metric}_exceeded", "metric": metric,
            "observed": value, "limit": limit, "comparator": comparator,
            "unit": "bytes" if "bytes" in metric else "px" if metric in {"pixels", "min_edge"} else "ratio",
            "limit_kind": kind, "severity": "warning", "status": "blocked", "action": "blocked",
            "source_url": limits["source_url"] if kind != "local_guardrail" else None,
            "verified_at": limits["verified_at"] if kind != "local_guardrail" else None,
            "client_resized": False, "limits_profile_version": limits["limits_profile_version"]}
        finding["finding_id"] = digest([identity, asset["original_sha256"], limits["limits_profile_sha256"], metric, kind])
        findings.append(finding)
    return asset, findings


def request_budget(system: str, parts: list[dict], metadata: list[dict], limits: dict, profile,
                   *, request_kwargs: dict | None = None) -> dict:
    """Inspect all encoded request images; the token count is a conservative estimate.

    Actual billing comes from provider usage. Reports contain hashes/dimensions,
    never Base64 content, so they are safe to persist on both success and failure.
    """
    q, s = profile.query_images, profile.long_screenshot
    images = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    body = request_kwargs or {"model": limits.get("model"), "messages": [{"role": "system", "content": system},
            {"role": "user", "content": parts}], "stream": True}
    if not request_kwargs and limits.get("vl_high_resolution_images"):
        body["vl_high_resolution_images"] = True
    serialized = json.dumps(body, ensure_ascii=False).encode()
    payload = len(serialized)
    text_tokens = len((system + "".join(p.get("text", "") for p in parts)).encode())
    views, reasons = [], []
    for index, url in enumerate(images):
        check_preparation()
        if not url.startswith("data:") or "," not in url:
            raise QueryImageError("request_image_invalid", "请求预算检查仅支持平台编码的本地图片")
        try:
            raw = base64.b64decode(url.split(",", 1)[1], validate=True)
            with Image.open(io.BytesIO(raw)) as im:
                width, height = im.size
        except Exception as exc:
            raise QueryImageError("request_image_invalid", "请求图片无法解码，不能计算完整预算") from exc
        identity = metadata[index] if index < len(metadata) else {}
        image_cfg = q if identity.get("image_role") == "query_image" else s
        uri_bytes = len(url.encode())
        image_tokens = ((width + 31) // 32) * ((height + 31) // 32) + 256
        view = {key: identity[key] for key in (
            "product", "product_id", "turn_id", "turn_index", "image_role", "view_id", "query_image_id",
            "timestamp", "time", "source_video", "evidence_type", "index",
            "time_s", "frame_no", "source_video_sha256",
            "asset_id", "source_turn", "product_no", "request_role", "part_no", "part_count",
        ) if key in identity}
        view.update({"request_image_index": index + 1, "sha256": hashlib.sha256(raw).hexdigest(),
                     "width": width, "height": height, "file_bytes": len(raw),
                     "data_uri_bytes": uri_bytes, "estimated_visual_tokens": image_tokens})
        image_reasons = []
        if width * height > image_cfg.max_pixels:
            image_reasons.append("request_image_pixels_exceeded")
        if uri_bytes >= image_cfg.max_data_url_bytes:
            image_reasons.append("request_image_data_uri_bytes_exceeded")
        if min(width, height) < image_cfg.min_edge:
            image_reasons.append("request_image_min_edge_invalid")
        hard = limits.get("official_rules", {})
        if (hard.get("data_uri_bytes") and uri_bytes > hard["data_uri_bytes"]
                or hard.get("file_bytes") and len(raw) > hard["file_bytes"]):
            image_reasons.append("request_image_official_bytes_exceeded")
        view["blocking_reasons"] = image_reasons
        reasons.extend(reason for reason in image_reasons if reason not in reasons)
        views.append(view)
    visual_tokens = sum(v["estimated_visual_tokens"] for v in views)
    tokens = text_tokens + visual_tokens
    reserve = max(q.output_reserve_tokens, s.output_reserve_tokens)
    declared = limits.get("model_declared_limits") or {}
    max_input = min(q.max_input_tokens, s.max_input_tokens, declared.get("max_input_tokens") or q.max_input_tokens)
    context_window = min(q.context_window, s.context_window, declared.get("context_window") or q.context_window)
    max_images = min(q.max_request_images, limits.get("official_rules", {}).get("request_images", q.max_request_images))
    effective = {"max_input_tokens": max_input, "context_window": context_window,
                 "output_reserve_tokens": reserve, "max_request_images": max_images,
                 "max_request_bytes": q.max_request_bytes, "request_overhead_bytes": q.request_overhead_bytes}
    if len(images) > max_images:
        reasons.append("request_image_count_exceeded")
    if payload + q.request_overhead_bytes > q.max_request_bytes:
        reasons.append("request_payload_bytes_exceeded")
    if tokens > max_input or tokens + reserve > context_window:
        reasons.append("context_budget_exceeded")
    report = {"actual_image_count": len(images), "actual_data_uri_bytes": sum(v["data_uri_bytes"] for v in views),
            "request_payload_bytes": payload, "request_overhead_reserve_bytes": q.request_overhead_bytes,
            "estimated_input_tokens": tokens, "estimated_text_tokens": text_tokens,
            "estimated_visual_tokens": visual_tokens, "output_reserve_tokens": reserve,
            "estimated_total_with_output_reserve": tokens + reserve,
            "max_input_tokens": max_input, "context_window": context_window,
            "token_estimation_method": "utf8_text_bytes_plus_32px_image_patches_and_256_per_image",
            "token_estimation_is_billing": False, "model": limits.get("model"),
            "effective_limits": effective, "model_declared_limits": declared,
            "limit_sources": {"query_images": q.model_dump(), "long_screenshot": s.model_dump(),
                              "model": limits.get("model_token_limits_origin", "visual_profile_local_guardrails")},
            "limits_profile_version": limits.get("limits_profile_version", "unverified-v1"),
            "limits_profile_sha256": limits.get("limits_profile_sha256"),
            "budget_algorithm_version": REQUEST_BUDGET_VERSION,
            "request_sha256": hashlib.sha256(serialized).hexdigest(), "image_views": views,
            "blocked": bool(reasons), "blocking_reasons": reasons,
            "retry_allowed": not bool(reasons), "automatic_retry_skipped": bool(reasons)}
    report["report_id"] = digest(report)
    return report


def enforce_request_budget(report: dict) -> None:
    """Raise a permanent, traceable local error without discarding any evidence."""
    if not report.get("blocked"):
        return
    reasons = report.get("blocking_reasons", [])
    context = "context_budget_exceeded" in reasons
    code = "context_budget_exceeded" if context else "request_image_limit_exceeded"
    caps = report.get("effective_limits", {})
    detail = (f"保守估算输入 {report.get('estimated_input_tokens')} Token，输入上限 {caps.get('max_input_tokens')}，"
              f"含输出预留 {report.get('estimated_total_with_output_reserve')}，上下文上限 {caps.get('context_window')}")
    exc = QueryImageError(code, "完整图文请求超过模型或平台保护限制：" + ", ".join(reasons) + "；" + detail + "；已停止自动重试",
                         request_budget_report=report)
    from .request_errors import request_limit_diagnostics
    error = request_limit_diagnostics(exc, model=report.get("model"))
    exc.request_limit_error = error
    if context:
        exc.context_limit_error = error
    raise exc


def declared_request_budget(kwargs: dict, judge) -> dict | None:
    """Guard every judge/repair call when an operator declared model token caps."""
    caps = {key: getattr(judge, key, None) for key in ("max_input_tokens", "context_window")}
    if not any(caps.values()):
        return None
    from types import SimpleNamespace
    from .config import QueryImageConfig
    # Other image guardrails belong to the visual profile and are checked by its
    # request_budget call. This boundary only applies the declared token caps.
    unbounded = 2**63 - 1
    reserve = kwargs.get("max_completion_tokens") or kwargs.get("max_tokens") or 8192
    cfg = QueryImageConfig(max_input_tokens=caps["max_input_tokens"] or unbounded,
                           context_window=caps["context_window"] or unbounded,
                           output_reserve_tokens=reserve, min_edge=1,
                           max_pixels=unbounded, max_data_url_bytes=unbounded,
                           max_request_images=unbounded, max_request_bytes=unbounded)
    system, parts = [], []
    for message in kwargs.get("messages", []):
        content = message.get("content") or ""
        if message.get("role") == "system" and isinstance(content, str):
            system.append(content)
        elif isinstance(content, str):
            parts.append({"type": "text", "text": content})
        elif isinstance(content, list):
            parts.extend(content)
        if message.get("tool_calls"):
            parts.append({"type": "text", "text": json.dumps(message["tool_calls"], ensure_ascii=False)})
    for key in ("tools", "response_format"):
        if kwargs.get(key):
            parts.append({"type": "text", "text": json.dumps(kwargs[key], ensure_ascii=False)})
    limits = {"model": kwargs.get("model"), "model_declared_limits": caps,
              "model_token_limits_origin": "operator_declared_judge_config",
              "limits_profile_version": "declared-judge-token-caps-v1"}
    limits["limits_profile_sha256"] = digest(limits)
    return request_budget("\n".join(system), parts, [], limits,
                          SimpleNamespace(query_images=cfg, long_screenshot=cfg), request_kwargs=kwargs)
