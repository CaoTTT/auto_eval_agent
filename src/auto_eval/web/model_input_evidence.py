"""Export only the frozen images belonging to each case's successful result."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import zipfile

from .screenshot_evidence import evidence_bytes


class EvidenceUnavailable(ValueError):
    """A recorded successful request cannot be exported faithfully."""


def successful_records(snapshot: dict) -> tuple[list[dict], list[dict]]:
    latest = {}
    for position, result in enumerate(snapshot.get("results") or []):
        raw_index = result.get("index", position)
        if isinstance(raw_index, bool):
            continue
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        latest[index] = result
    records, skipped = [], []
    for index, item in enumerate(snapshot.get("items") or []):
        identity = {"item_index": index, "item_id": item.get("id") or f"q{index}",
                    "query": item.get("query") or item.get("question") or "",
                    "session_id": item.get("session_id"), "target_turn": item.get("turn_index")}
        result = latest.get(index)
        reason = "not_evaluated" if result is None else "evaluation_failed" if result.get("error") else ""
        if result and (result.get("evaluation_pending") or result.get("_paused")):
            reason = "not_evaluated"
        record = result.get("model_input_evidence") if result else None
        if not reason and not record:
            # Older screenshot audits already freeze the exact request bytes.
            # Never reconstruct old video requests from current frame files.
            legacy = item.get("screenshot_evidence") or {}
            if (legacy.get("record_status") == "model_response_received"
                    and not legacy.get("request_id")
                    and legacy.get("images")
                    and all(image.get("evidence_path") for image in legacy["images"])):
                record = {key: legacy.get(key) for key in
                          ("request_sha256", "judge_model", "images")}
                record.update(version="screenshot-evidence-1", evidence_mode="long_screenshot",
                              record_status="evaluation_succeeded")
            else:
                reason = "request_not_recorded"
        if not reason and (record.get("record_status") != "evaluation_succeeded" or not record.get("images")):
            reason = "request_not_recorded"
        if reason:
            skipped.append({**identity, "reason": reason})
        else:
            records.append({**deepcopy(record), **identity})
    return records, skipped


def write_model_input_zip(snapshot: dict, destination: Path) -> Path:
    records, skipped = successful_records(snapshot)
    if not records:
        raise ValueError("没有可导出的成功评测请求图片；旧录屏任务需重新评估后才能导出。")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for record in records:
                for image_no, image in enumerate(record["images"], 1):
                    if not image.get("evidence_path"):
                        raise EvidenceUnavailable("请求图片未保存，无法导出完整证据包。")
                    try:
                        raw, mime = evidence_bytes(image)
                    except (OSError, ValueError) as exc:
                        raise EvidenceUnavailable(
                            f"第{record['item_index'] + 1}题第{image_no}张请求图片缺失或已变化，无法导出完整证据包。"
                        ) from exc
                    suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[mime]
                    name = f"case_{record['item_index'] + 1:04d}/image_{image_no:04d}{suffix}"
                    archive.writestr(name, raw)
                    image.update(image_no=image_no, archive_path=name)
            archive.writestr("manifest.json", json.dumps({
                "version": "model-input-evidence-1", "task_id": snapshot.get("task_id"),
                "dataset_name": snapshot.get("dataset_name", ""),
                "total_cases": len(snapshot.get("items") or []), "exported_cases": len(records),
                "image_count": sum(len(record["images"]) for record in records),
                "records": records, "skipped_cases": skipped,
            }, ensure_ascii=False, indent=2))
            archive.writestr("README.txt",
                "本包仅包含本任务各 Case 最新成功评测实际发送给裁判的图片，按请求顺序编号。\n"
                "录屏使用发送时编码后的采样帧；长截图使用实际原图或切片，不额外附带未发送的完整原图。\n"
                "图文题含模型实际使用的提问图片；多轮题含随该轮请求发送的历史与当前图片。\n"
                "manifest.json 标明 Case、产品、来源轮次、图片角色、顺序、SHA256 和跳过原因。\n"
                "失败、未评测及未记录实际请求图片的旧 Case 不含图片，详见 skipped_cases。\n")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination
