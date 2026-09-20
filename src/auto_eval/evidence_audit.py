"""Exact, Base64-free screenshot request records for replay and inspection."""
from copy import deepcopy
import base64
import hashlib
import json

from .paths import RUNS_DIR
from .query_images import _snapshot_bytes

VERSION = "screenshot-evidence-1"


def capture_request(system: str, parts: list[dict], metadata: list[dict],
                    preprocessing: list[dict], protocol: dict) -> dict:
    images, sequence = [], []
    for part in parts:
        if part["type"] == "text":
            sequence.append({"type": "text", "text": part["text"]})
            continue
        index = len(images)
        raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1], validate=True)
        meta = deepcopy(metadata[index])
        meta.pop("preprocessing", None)
        meta.update(image_no=index + 1, sha256=hashlib.sha256(raw).hexdigest(),
                    file_bytes=len(raw), data_url_bytes=len(part["image_url"]["url"].encode()),
                    mime=part["image_url"]["url"].split(";", 1)[0][5:])
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[meta["mime"]]
        frozen = RUNS_DIR / "screenshot_evidence" / (meta["sha256"] + suffix)
        frozen.parent.mkdir(parents=True, exist_ok=True)
        _snapshot_bytes(frozen, raw)
        meta["evidence_path"] = str(frozen.resolve())
        images.append(meta)
        sequence.append({"type": "image", "image_no": index + 1,
                         "image_options": {k: v for k, v in part["image_url"].items() if k != "url"}})
    record = {"version": VERSION, "record_status": "assembled", "protocol": protocol,
            "composition": "ordered_images_without_pixel_stitching", "system_prompt": system,
            "content_sequence": sequence, "images": images,
            "preprocessing": deepcopy(preprocessing)}
    record["request_sha256"] = hashlib.sha256(json.dumps({"system": system, "sequence": sequence,
        "images": [image["sha256"] for image in images]}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return record
