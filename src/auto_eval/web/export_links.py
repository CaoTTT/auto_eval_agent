"""Generate only task-bound HTTP links, never filesystem or spreadsheet formulas."""
from __future__ import annotations

import os
from urllib.parse import quote, urlsplit


class ExportLink(str):
    """Trusted generated URL; untrusted input strings remain ordinary cells."""


def export_base_url(request_url: str = "") -> str:
    base = (os.environ.get("EXPORT_PUBLIC_BASE_URL") or request_url).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return ""
    return base


def screenshot_links(snapshot: dict, item: dict, index: int) -> dict:
    base = export_base_url(snapshot.get("export_base_url", ""))
    task_id = snapshot.get("task_id")
    if not base or not task_id:
        return {}
    source = item.get("source_data") or {}
    count = item.get("product_count") or (3 if item.get("screenshot3") or source.get("screenshot3") else 2)
    links = {}
    for product in range(1, min(int(count), 3) + 1):
        meta = item.get(f"screenshot_meta{product}") or {}
        if meta.get("original_path") or item.get(f"screenshot{product}") or source.get(f"screenshot{product}"):
            links[f"产品{product}长截图查看链接"] = ExportLink(
                f"{base}/api/eval/{quote(str(task_id), safe='')}/items/{index}/screenshots/{product}")
    return links
