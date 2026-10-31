"""Configured image/request diagnostics independent of the API domain."""
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

PROFILE_VERSION = "local-config-v1"


def resolve_image_limits(judge, profile) -> dict:
    host = (urlparse(judge.base_url or "").hostname or "").lower()
    result = {"provider": "configured", "model": judge.model,
              "endpoint_host": host, "api_family": judge.runner, "transport": "base64",
              # Kept for compatibility with frozen manifests. "Verified" no longer
              # gates an endpoint, because custom OpenAI-compatible gateways are valid.
              "capability_verified": False, "effective_pixel_cap": None,
              "vl_high_resolution_images": judge.vl_high_resolution_images,
              "official_rules": {},
              "source_url": None, "verified_at": None,
              "byte_interpretation": "local visual profile; provider limits are not asserted",
              "local_guardrails": profile.query_images.model_dump(),
              "answer_guardrails": profile.long_screenshot.model_dump(),
              "limits_profile_version": PROFILE_VERSION}
    result["limits_profile_sha256"] = digest(result)
    return result


def effective_profile(profile, limits: dict):
    """Apply frozen hard rules when present, otherwise use local configuration."""
    cap = limits.get("effective_pixel_cap")
    hard = limits.get("official_rules") or {}
    hard_file_bytes = hard.get("file_bytes")
    hard_data_uri_bytes = hard.get("data_uri_bytes")
    hard_request_images = hard.get("request_images")
    hard_min_edge = hard.get("min_edge")
    q = profile.query_images.model_copy(update={
        "max_pixels": min(profile.query_images.max_pixels, cap) if cap else profile.query_images.max_pixels,
        "max_file_bytes": min(profile.query_images.max_file_bytes, hard_file_bytes)
            if hard_file_bytes else profile.query_images.max_file_bytes,
        "max_data_url_bytes": min(profile.query_images.max_data_url_bytes, hard_data_uri_bytes + 1)
            if hard_data_uri_bytes else profile.query_images.max_data_url_bytes,
        "max_request_images": min(profile.query_images.max_request_images, hard_request_images)
            if hard_request_images else profile.query_images.max_request_images,
        "min_edge": max(profile.query_images.min_edge, hard_min_edge)
            if hard_min_edge else profile.query_images.min_edge,
    })
    s = profile.long_screenshot.model_copy(update={
        "max_pixels": min(profile.long_screenshot.max_pixels, cap)
            if cap else profile.long_screenshot.max_pixels,
        "max_data_url_bytes": min(
            profile.long_screenshot.max_data_url_bytes,
            hard_data_uri_bytes + 1 if hard_data_uri_bytes else profile.long_screenshot.max_data_url_bytes,
            data_url_size(hard_file_bytes) + 1 if hard_file_bytes else profile.long_screenshot.max_data_url_bytes,
        ),
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
             for key, value in (limits.get("official_rules") or {}).items() if key in observed]
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
            "source_url": limits.get("source_url") if kind != "local_guardrail" else None,
            "verified_at": limits.get("verified_at") if kind != "local_guardrail" else None,
            "client_resized": False, "limits_profile_version": limits["limits_profile_version"]}
        finding["finding_id"] = digest([identity, asset["original_sha256"], limits["limits_profile_sha256"], metric, kind])
        findings.append(finding)
    return asset, findings


def request_budget(system: str, parts: list[dict], metadata: list[dict], limits: dict, profile) -> dict:
    q, s = profile.query_images, profile.long_screenshot
    images = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    body = {"model": limits["model"], "messages": [{"role": "system", "content": system},
            {"role": "user", "content": parts}], "stream": True}
    if limits["vl_high_resolution_images"]:
        body["vl_high_resolution_images"] = True
    payload = len(json.dumps(body, ensure_ascii=False).encode())
    tokens = len((system + "".join(p.get("text", "") for p in parts)).encode())
    for url in images:
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1], validate=True))) as im:
            tokens += ((im.width + 31) // 32) * ((im.height + 31) // 32) + 256
    reserve = max(q.output_reserve_tokens, s.output_reserve_tokens)
    reasons = []
    if len(images) > q.max_request_images:
        reasons.append("request_image_count_exceeded")
    if payload + q.request_overhead_bytes > q.max_request_bytes:
        reasons.append("request_payload_bytes_exceeded")
    if tokens > min(q.max_input_tokens, s.max_input_tokens) or tokens + reserve > min(q.context_window, s.context_window):
        reasons.append("context_budget_exceeded")
    return {"actual_image_count": len(images), "actual_data_uri_bytes": sum(len(u.encode()) for u in images),
            "request_payload_bytes": payload, "request_overhead_reserve_bytes": q.request_overhead_bytes,
            "estimated_input_tokens": tokens, "output_reserve_tokens": reserve,
            "limits_profile_version": limits["limits_profile_version"], "blocked": bool(reasons), "blocking_reasons": reasons}
