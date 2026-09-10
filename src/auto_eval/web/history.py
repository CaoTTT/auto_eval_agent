"""Web 评测历史持久化与完整导出。

这里刻意不用数据库：评测台是本地/轻量服务，JSON 快照足够支撑历史加载；
XLSX 直接生成 OOXML，避免给项目额外引入 openpyxl / xlsxwriter 依赖。
"""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import re
import time
import threading
import uuid
import zipfile
from datetime import datetime
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from ..paths import PROJECT_ROOT, RUNS_DIR
from .xlsx_images import CELL_IMAGE_REL, CellImage, OriginalImageError, WpsCellImages


HISTORY_DIR = RUNS_DIR / "web_history"
logger = logging.getLogger(__name__)
_xlsx_slot = threading.BoundedSemaphore(1)


def _safe_name(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]", "_", value)


def _task_id_slug(task_id: str) -> str:
    """task_id 的文件名安全形态；净化有损（如中文/符号 id）时追加短哈希。

    不追加哈希的话，不同原始 id 净化后同名（如同长度中文 id），同一秒创建
    的两个任务会生成相同 session_name、快照文件互相覆盖。
    """
    safe = _safe_name(task_id)
    if safe != task_id:
        digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:6]
        safe = f"{safe}-{digest}"
    return safe


def make_session_name(created_at: float, mode: str, task_id: str) -> str:
    """生成可按文件名排序、同时能关联任务的稳定会话名。"""
    dt = datetime.fromtimestamp(created_at).astimezone()
    return f"{dt:%Y%m%d_%H%M%S}_{_safe_name(mode)}_{_task_id_slug(task_id)}"


def _snapshot_task_id_matches(path: Path, task_id: str) -> bool:
    """校验快照 JSON 内的 task_id 与请求的原始 id 一致。

    _safe_name 会把非 [0-9a-zA-Z_-] 字符统一替换成 "_"：放开 task_id 字符集后，
    不同原始 id 可能净化成同一文件名模式（如同长度中文 id）。glob/旧文件名命中
    后必须读字段校验，只认真正属于该 id 的快照；旧快照缺 task_id 字段时保留
    原命中以兼容。
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    stored = data.get("task_id")
    return stored is None or stored == task_id


def _find_task_path(task_id: str) -> Path:
    """优先查旧文件名，再查带时间前缀的新文件名。

    新文件名用 _task_id_slug（有损 id 带短哈希），命中后均校验快照内的
    task_id 字段（见 _snapshot_task_id_matches）；全部不一致时返回不存在
    的旧路径，调用方按"任务不存在"处理。
    """
    legacy = HISTORY_DIR / f"{_safe_name(task_id)}.json"
    if legacy.exists() and _snapshot_task_id_matches(legacy, task_id):
        return legacy
    matches = sorted(HISTORY_DIR.glob(f"*_{_task_id_slug(task_id)}.json"))
    verified = [p for p in matches if _snapshot_task_id_matches(p, task_id)]
    return verified[-1] if verified else legacy


def _task_path(task_id: str, session_name: str = "") -> Path:
    if session_name:
        return HISTORY_DIR / f"{_safe_name(session_name)}.json"
    return _find_task_path(task_id)


def task_to_snapshot(task) -> dict:
    return {
        "task_id": task.id,
        "session_name": task.session_name,
        "mode": task.mode,
        "dataset_name": getattr(task, "dataset_name", ""),
        "note": getattr(task, "note", ""),
        "items": task.items,
        "options": task.options,
        "evaluation_profile": getattr(task, "evaluation_profile", ""),
        "protocol_manifest": getattr(task, "protocol_manifest", {}),
        "status": task.status,
        "results": task.results,
        "item_progress": task.item_progress,
        "progress_events": task.progress_events,
        "summary": task.summary,
        "created_at": task.created_at,
        "updated_at": time.time(),
        "done_total": task.done_total,
        "error": task.error,
        "repair_status": getattr(task, "repair_status", "idle"),
        "retry_runs": getattr(task, "retry_runs", {}),
    }


def save_task(task, *, max_attempts: int = 3) -> bool:
    """Best-effort atomic snapshot save.

    Snapshot persistence must never terminate an evaluation.  A unique
    temporary file avoids concurrent writers sharing the same ``.tmp`` path;
    short retries cover transient Windows file locks.
    """
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _task_path(task.id, getattr(task, "session_name", ""))
    snapshot = task_to_snapshot(task)
    content = json.dumps(snapshot, ensure_ascii=False, indent=2)
    last_error: OSError | None = None
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            _write_meta(path, _snapshot_meta_row(snapshot, path))
            return True
        except OSError as exc:
            last_error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt + 1 < attempts:
                time.sleep(0.02 * (attempt + 1))
    logger.error(
        "保存任务快照失败: task_id=%s attempts=%s error=%s",
        getattr(task, "id", "-"),
        attempts,
        last_error,
    )
    return False


def load_snapshot(task_id: str) -> dict | None:
    path = _find_task_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_snapshot(task_id: str) -> bool:
    """删除某次评测的快照文件（连同摘要侧车）。返回是否删除成功。"""
    path = _find_task_path(task_id)
    if not path.exists():
        return False
    try:
        path.unlink()
    except Exception:
        return False
    try:
        _meta_path(path).unlink(missing_ok=True)
    except Exception:
        pass
    return True


_META_SUFFIX = ".meta.json"


def _meta_path(path: Path) -> Path:
    """快照 X.json 的摘要侧车路径（X.json.meta.json）。

    侧车与主快照同名前缀，列表页只读它（几百字节）而不必把每个历史快照
    全量 read_text + json.loads——快照随使用时间累积后那是数百 MB 级的
    瞬时内存分配。侧车名以 ".meta.json" 结尾，_find_task_path 的
    "*_{slug}.json" glob 不会误命中（结尾是 "." 不是 "_"）。
    """
    return path.with_name(path.name + _META_SUFFIX)


def _apply_interrupted_status(status, error):
    """盘上停在 pending/queued/running 只可能是服务中断，统一改写为 error。"""
    if status in {"pending", "queued", "running"}:
        return "error", error or "服务中断，已保留中断前完成的评估结果"
    return status, error


def _snapshot_evaluation_profile(data: dict) -> str:
    explicit = data.get("evaluation_profile") or (data.get("options") or {}).get(
        "evaluation_profile"
    )
    if explicit or data.get("mode") != "compare":
        return explicit or ""
    versions = [
        str((data.get("summary") or {}).get("standard_version") or ""),
        *[
            str(result.get("standard_version") or "")
            for result in (data.get("results") or [])
        ],
    ]
    return (
        "qa_competitor_compare@0.3"
        if "0.3" in versions
        else "qa_competitor_compare@0.2-simplified"
    )


def _snapshot_meta_row(data: dict, path: Path) -> dict:
    """从完整快照 dict 计算历史列表行（摘要字段），存原始 status/error。"""
    task_id = data.get("task_id") or path.stem
    created_at = data.get("created_at")
    session_name = data.get("session_name") or (
        path.stem
        if path.stem != _safe_name(str(task_id))
        else make_session_name(float(created_at or 0), data.get("mode") or "unknown", str(task_id))
    )
    latest_results: dict[int, dict] = {}
    for position, result in enumerate(data.get("results") or []):
        try:
            index = int(result.get("index", position))
        except (TypeError, ValueError):
            continue
        latest_results[index] = result
    return {
        "task_id": task_id,
        "session_name": session_name,
        "dataset_name": data.get("dataset_name") or "",
        "note": data.get("note") or "",
        "mode": data.get("mode"),
        "status": data.get("status"),
        "total": len(data.get("items") or []),
        "done": len([r for r in latest_results.values() if not r.get("error")]),
        "created_at": created_at,
        "updated_at": data.get("updated_at") or data.get("created_at"),
        "error": data.get("error"),
        "repair_status": data.get("repair_status") or "idle",
        "evaluation_profile": _snapshot_evaluation_profile(data),
        "preview": _preview(data),
        "meta_version": 1,
    }


def _write_meta(snapshot_path: Path, row: dict) -> None:
    """原子写摘要侧车；best-effort，失败仅告警不影响主快照。"""
    meta_path = _meta_path(snapshot_path)
    tmp = meta_path.with_name(f".{meta_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, meta_path)
    except OSError as exc:
        logger.warning(
            "历史摘要侧车写入失败: %s error=%s", meta_path.name, exc
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _load_meta_row(path: Path) -> dict | None:
    """读单个快照的列表行：优先侧车；miss/损坏则全量读主快照一次并补写
    侧车（legacy 自愈）；两者都不可读返回 None（跳过该条）。"""
    row: dict | None = None
    meta_path = _meta_path(path)
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                row = data
        except Exception:
            row = None
    if row is None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        row = _snapshot_meta_row(data, path)
        _write_meta(path, row)
    row.pop("meta_version", None)  # 版本号只落侧车，响应形状与旧实现一致
    status, error = _apply_interrupted_status(row.get("status"), row.get("error"))
    row["status"] = status
    row["error"] = error
    if row.get("repair_status") in {"queued", "running"}:
        row["repair_status"] = "error"
    return row


def list_snapshots(limit: int = 50) -> list[dict]:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in HISTORY_DIR.glob("*.json"):
        if path.name.endswith(_META_SUFFIX):
            continue
        row = _load_meta_row(path)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return rows[:limit]

def _preview(data: dict) -> str:
    items = data.get("items") or []
    if not items:
        return ""
    q = str(items[0].get("query") or "")
    return q[:80] + ("…" if len(q) > 80 else "")


def snapshot_payload(data: dict) -> dict:
    return {
        "task_id": data.get("task_id"),
        "session_name": data.get("session_name"),
        "dataset_name": data.get("dataset_name") or "",
        "note": data.get("note") or "",
        "mode": data.get("mode"),
        "items": data.get("items") or [],
        "options": data.get("options") or {},
        "evaluation_profile": _snapshot_evaluation_profile(data),
        "protocol_manifest": data.get("protocol_manifest") or {},
        "status": data.get("status"),
        "results": data.get("results") or [],
        "item_progress": data.get("item_progress") or {},
        "progress_events": data.get("progress_events") or {},
        "summary": data.get("summary") or {},
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "error": data.get("error"),
        "repair_status": data.get("repair_status") or "idle",
        "retry_runs": data.get("retry_runs") or {},
    }


def export_rows(snapshot: dict) -> dict[str, list[dict]]:
    """把一次评测拆成多个 Sheet 的行数据。

    ``数据集明细`` 与 ``逐题结果`` 都以原始 items 为主表，严格按输入顺序
    一一对齐。并发评测导致的完成顺序变化不会影响导出；失败或待评估条目
    仍占据原行，只将评分字段留空。

    rich_content 与 compare 均按对外定名列导出（同时供单条结果查询复用）；
    其余（已删模式的旧历史快照）按维度展开成独立列（维度_X / 理由_X）兜底。
    """
    results = _results_with_identity(snapshot)
    aligned_results = _aligned_results(snapshot, results)
    summary = snapshot.get("summary") or {}
    mode = snapshot.get("mode")

    if mode == "rich_content":
        result_rows = _rich_content_export_rows(aligned_results)
    elif mode == "compare":
        result_rows = _visual_compare_export_rows(aligned_results)
    else:
        result_rows = _result_rows(aligned_results)
    rows: dict[str, list[dict]] = {
        "数据集明细": _dataset_rows(snapshot),
        "逐题结果": result_rows,
    }
    frame_rows = _frame_manifest_rows(snapshot)
    if frame_rows:
        has_screenshots = any(item.get("evidence_mode") == "long_screenshot" for item in snapshot.get("items", []))
        rows["视觉证据清单" if has_screenshots else "抽帧清单"] = frame_rows
    rows["运行信息"] = [_run_info(snapshot)]
    if summary:
        rows["汇总指标"] = [_flatten_dict(summary, skip_keys={"by_category"})]
    return rows


def _results_with_identity(snapshot: dict) -> list[dict]:
    """为历史失败结果回填题号和 query，兼容早期不完整快照。"""
    items = snapshot.get("items") or []
    results: list[dict] = []
    for position, result in enumerate(snapshot.get("results") or []):
        row = dict(result)
        raw_index = row.get("index", position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        item = items[index] if 0 <= index < len(items) else {}
        if not row.get("item_id"):
            row["item_id"] = item.get("id") or f"q{index}"
        if not row.get("query"):
            row["query"] = item.get("query") or item.get("question") or ""
        row["评估状态"] = "评估失败" if row.get("error") else "已完成"
        results.append(row)
    return results


def _aligned_results(snapshot: dict, results: list[dict]) -> list[dict]:
    """按输入 items 左连接结果；运行中/失败条目也保留固定行位。"""
    items = snapshot.get("items") or []
    if not items:
        return results

    by_index: dict[int, dict] = {}
    by_item_id: dict[str, dict] = {}
    for result in results:
        try:
            index = int(result.get("index"))
        except (TypeError, ValueError):
            index = -1
        if index >= 0:
            by_index[index] = result
        item_id = str(result.get("item_id") or "").strip()
        if item_id:
            by_item_id[item_id] = result

    aligned: list[dict] = []
    progress = snapshot.get("item_progress") or {}
    for index, item in enumerate(items):
        item_id = str(item.get("id") or f"q{index}")
        result = by_index.get(index) or by_item_id.get(item_id)
        if result is not None:
            export_row = {
                "数据集序号": index + 1,
                "index": index,
                "item_id": item_id,
                "query": item.get("query") or item.get("question") or "",
            }
            export_row.update(result)
            aligned.append(export_row)
            continue
        progress_row = progress.get(str(index)) or progress.get(index) or {}
        status = progress_row.get("status")
        export_status = "评估失败" if status == "error" else "待评估"
        aligned.append({
            "数据集序号": index + 1,
            "index": index,
            "item_id": item_id,
            "query": item.get("query") or item.get("question") or "",
            "context": item.get("context") or "",
            "评估状态": export_status,
            "error": progress_row.get("error") or (
                progress_row.get("message") if status == "error" else ""
            ),
        })
    return aligned


_RUNTIME_ITEM_FIELDS = {
    "evidence_mode", "screenshot_meta1", "screenshot_meta2", "screenshot_meta3",
    "frames",
    "frames1",
    "frames2",
    "frames3",
    "frame_count",
    "media",
    "video_name",
    "video1_path",
    "video2_path",
    "video3_path",
    "duration",
    "duration1",
    "duration2",
    "duration3",
    "source_data",
}

# 垂域视觉评测（rich_content）Excel/CSV 导出列：按此顺序输出。
# query_id / 垂域分类 / 卡片存在情况 / correctness / error_type 为外部对接定名；
# Superlink 合适度、回答覆盖、耗时不再导出。
_RICH_CONTENT_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("item_id", "query_id"),
    ("query", "Query"),
    ("context", "context"),
    ("category_display", "垂域分类"),
    ("answer_text", "answer_text"),
    ("card_presence_label", "卡片存在情况"),
    ("card_count", "卡片数量"),
    ("card_types", "卡片种类"),
    ("card_contents", "卡片内容"),
    ("superlink_presence_label", "Superlink存在情况"),
    ("superlink_count", "Superlink数量"),
    ("superlink_texts", "Superlink文字"),
    ("card_suitability", "卡片是否合适"),
    ("card_suitability_reason", "卡片不合适原因"),
    ("needs_review_label", "识别是否需要人工复查"),
    ("review_reason", "需要复核的原因"),
    ("problem_solved", "correctness"),
    ("problem_solved_reason", "评价的原因"),
    ("answer_issues", "error_type"),
    ("rationale", "识别结论"),
    ("analysis", "评价分析过程"),
]

# 列表类字段取值后需要拼接为字符串
_RICH_CONTENT_LIST_FIELDS = {"card_types", "card_contents", "superlink_texts"}

# 枚举值 → 展示值映射
_RICH_CONTENT_DISPLAY_MAP: dict[str, dict[str, str]] = {
    "card_suitability": {"ok": "OK", "nok": "NOK"},
    "problem_solved": {"ok": "OK", "nok": "NOK", "need_review": "需复查"},
}


def _rich_content_export_rows(results: list[dict]) -> list[dict]:
    """将 rich_content 结果行按导出列顺序重排并转换为对外定名（见列定义注释）。"""
    export: list[dict] = []
    for row in results:
        export_row: dict[str, Any] = {}
        for key, label in _RICH_CONTENT_EXPORT_COLUMNS:
            value = row.get(key)
            if value is None:
                value = ""
            if key in _RICH_CONTENT_LIST_FIELDS and isinstance(value, list):
                value = "；".join(str(v) for v in value)
            if key in _RICH_CONTENT_DISPLAY_MAP and value:
                value = _RICH_CONTENT_DISPLAY_MAP[key].get(str(value), value)
            export_row[label] = value
        export.append(export_row)
    return export


def _project_relative_path(
    value: Any,
    project_root: Path | None = None,
) -> str:
    """把项目内路径转为稳定的 POSIX 相对路径；项目外路径返回空。"""
    if value is None or str(value).strip() == "":
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    root = project_root or PROJECT_ROOT
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def _source_data_for_item(item: dict) -> dict:
    source = item.get("source_data")
    if isinstance(source, dict):
        return dict(source)
    # 旧历史没有 source_data：尽量从规范化 item 回填，不暴露运行时绝对帧列表。
    return {
        key: value
        for key, value in item.items()
        if key not in _RUNTIME_ITEM_FIELDS
    }


def _item_visual_streams(item: dict) -> list[dict[str, Any]]:
    """统一返回 rich_content 单路或 compare 双/三路视频及关键帧。"""
    source = _source_data_for_item(item)
    if item.get("evidence_mode") == "long_screenshot" or item.get("screenshot1") or source.get("screenshot1"):
        count = item.get("product_count") or (3 if item.get("screenshot3") or source.get("screenshot3") else 2)
        streams = []
        for product_no in range(1, count + 1):
            meta = item.get(f"screenshot_meta{product_no}") or {}
            original = meta.get("original_path") or item.get(f"screenshot{product_no}") or source.get(f"screenshot{product_no}") or ""
            streams.append({
                "product_no": product_no, "evidence_mode": "long_screenshot",
                "source_video": "", "runtime_video": "", "duration": "",
                "original_path": str(PROJECT_ROOT / original) if original else "",
                "screenshot_meta": meta,
                "frames": [PROJECT_ROOT / str(path) for path in item.get(f"frames{product_no}", [])],
            })
        return streams
    has_numbered_streams = any(
        item.get(field) not in (None, "", []) or source.get(field) not in (None, "", [])
        for field in ("video1", "video2", "video3", "frames1", "frames2", "frames3")
    )
    if not has_numbered_streams:
        media = item.get("media") or []
        return [{
            "evidence_mode": "video_frames",
            "product_no": None,
            "source_video": source.get("video_path") or item.get("video_path") or "",
            "runtime_video": item.get("video_path") or (media[0] if media else ""),
            "frames": [Path(str(path)) for path in (item.get("frames") or [])],
            "duration": item.get("duration") or "",
        }]

    product_count = item.get("product_count")
    if product_count not in (2, 3):
        product_count = 3 if any(
            item.get(field) not in (None, "", []) or source.get(field) not in (None, "", [])
            for field in ("video3", "frames3", "answer3", "context3")
        ) else 2
    media = item.get("media") or []
    streams: list[dict[str, Any]] = []
    for product_no in range(1, product_count + 1):
        streams.append({
            "evidence_mode": "video_frames",
            "product_no": product_no,
            "source_video": source.get(f"video{product_no}") or "",
            "runtime_video": item.get(f"video{product_no}_path") or (
                media[product_no - 1] if len(media) >= product_no else ""
            ),
            "frames": [
                Path(str(path))
                for path in (item.get(f"frames{product_no}") or [])
            ],
            "duration": item.get(f"duration{product_no}") or "",
        })
    return streams


def _dataset_rows(snapshot: dict) -> list[dict]:
    rows: list[dict] = []
    for index, item in enumerate(snapshot.get("items") or []):
        source = _source_data_for_item(item)
        row: dict[str, Any] = {
            "数据集序号": index + 1,
            "source_line": item.get("source_line") or index + 1,
            "id": item.get("id") or f"q{index}",
            "query": item.get("query") or item.get("question") or "",
        }
        for key, value in source.items():
            if key not in row:
                row[key] = value

        streams = _item_visual_streams(item)
        for stream in streams:
            product_no = stream["product_no"]
            prefix = f"产品{product_no}" if product_no is not None else ""
            frames = stream["frames"]
            if stream["evidence_mode"] == "long_screenshot":
                meta = stream["screenshot_meta"]
                row.update({
                    f"{prefix}原始长截图": meta.get("original_path", ""),
                    f"{prefix}视觉证据图片": "\n".join(part["path"] for part in meta.get("slices", [])),
                    f"{prefix}图片数量": len(frames),
                    f"{prefix}切分状态": meta.get("split_status", "未准备"),
                })
                continue
            frame_project_paths = [
                path for path in (
                    _project_relative_path(frame) for frame in frames
                ) if path
            ]
            frame_dir = (
                _project_relative_path(frames[0].parent) if frames else ""
            )
            row.update({
                f"{prefix}录屏项目相对路径": _project_relative_path(
                    stream["runtime_video"]
                ),
                f"{prefix}抽帧目录项目相对路径": frame_dir,
                f"{prefix}帧项目相对路径": "\n".join(frame_project_paths),
                f"{prefix}抽帧数量": len(frames),
                f"{prefix}录屏时长（秒）": stream["duration"],
            })
        rows.append(row)
    return rows


def _frame_metadata(frame_dir: Path) -> tuple[dict[int, dict], dict]:
    metadata_path = frame_dir / "keyframes.json"
    if not metadata_path.is_file():
        return {}, {}
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    selected = {
        int(row.get("index")): row
        for row in (metadata.get("selected") or [])
        if isinstance(row, dict) and isinstance(row.get("index"), int)
    }
    return selected, metadata


def _frame_manifest_rows(snapshot: dict) -> list[dict]:
    """生成一帧一行的导出清单；没有成功抽帧的条目也保留一行。"""
    rows: list[dict] = []
    for item_index, item in enumerate(snapshot.get("items") or []):
        streams = _item_visual_streams(item)
        if not any(
            stream["source_video"] or stream["runtime_video"] or stream["frames"] or stream.get("original_path")
            for stream in streams
        ):
            continue
        for stream in streams:
            product_no = stream["product_no"]
            frames = stream["frames"]
            if stream["evidence_mode"] == "long_screenshot":
                meta = stream["screenshot_meta"]
                for part_no, part in enumerate(meta.get("slices") or [{}], 1):
                    rows.append({
                        "数据集序号": item_index + 1, "id": item.get("id") or f"q{item_index}",
                        "产品序号": product_no, "图片序号": part_no,
                        "视觉证据路径": part.get("path", ""), "起始行": part.get("start_y"),
                        "结束行": part.get("end_y"), "切分状态": meta.get("split_status", "未准备"),
                    })
                continue
            selected, _ = _frame_metadata(frames[0].parent) if frames else ({}, {})
            base = {
                "数据集序号": item_index + 1,
                "id": item.get("id") or f"q{item_index}",
                "query": item.get("query") or item.get("question") or "",
                "产品序号": product_no or "",
                "录屏项目相对路径": _project_relative_path(stream["runtime_video"]),
                "原始video_path": stream["source_video"],
            }
            if not frames:
                rows.append({
                    **base,
                    "帧序号": "",
                    "帧项目相对路径": "",
                    "时间点": "",
                    "来源": "",
                    "保留原因": "",
                    "抽帧状态": "无抽帧结果",
                })
                continue
            for frame_index, frame in enumerate(frames, start=1):
                info = selected.get(frame_index) or {}
                rows.append({
                    **base,
                    "帧序号": frame_index,
                    "帧项目相对路径": _project_relative_path(frame),
                    "时间点": info.get("time", ""),
                    "来源": info.get("source", ""),
                    "保留原因": info.get("keep_reason", ""),
                    "抽帧状态": "已生成" if frame.is_file() else "文件缺失",
                })
    return rows


def _run_info(snapshot: dict) -> dict:
    created = snapshot.get("created_at")
    updated = snapshot.get("updated_at")
    return {
        "task_id": snapshot.get("task_id"),
        "dataset_name": snapshot.get("dataset_name") or "",
        "note": snapshot.get("note") or "",
        "mode": snapshot.get("mode"),
        "status": snapshot.get("status"),
        "total": len(snapshot.get("items") or []),
        "done": len([r for r in (snapshot.get("results") or []) if "error" not in r]),
        "created_at": _format_ts(created),
        "updated_at": _format_ts(updated),
        "options": snapshot.get("options") or {},
        "evaluation_profile": snapshot.get("evaluation_profile") or "",
        "protocol_manifest": snapshot.get("protocol_manifest") or {},
        "error": snapshot.get("error") or "",
    }


def _format_ts(value) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")

def _visual_compare_export_rows(results: list[dict]) -> list[dict]:
    """垂域视觉对比导出：维度对比结论 + 内容冲突。"""
    _COMPARE_COLUMNS: list[tuple[str, str]] = [
        ("item_id", "query_id"),
        ("query", "题目"),
        ("context", "背景"),
        ("context1", "产品1背景"),
        ("answer1", "产品1回答"),
        ("context2", "产品2背景"),
        ("answer2", "产品2回答"),
        ("context3", "产品3背景"),
        ("answer3", "产品3回答"),
        ("relevance", "相关性"),
        ("safety", "安全合规"),
        ("content_quality", "内容质量"),
        ("need_closure", "需求闭环"),
        ("personalization", "个性化一致性"),
        ("has_conflict", "内容冲突"),
        ("conflict_reason", "内容冲突理由"),
        ("rationale", "理由"),
        ("standard_id", "标准ID"),
        ("standard_version", "标准版本"),
        ("evaluation_profile", "评测协议"),
        ("bundle_revision", "协议包修订"),
        ("prompt_sha256", "Prompt SHA256"),
        ("product_count", "产品数量"),
        ("answer1_input_status", "产品1输入状态"),
        ("answer2_input_status", "产品2输入状态"),
        ("answer3_input_status", "产品3输入状态"),
        ("answer1_response_gate", "产品1响应体验Gate"),
        ("answer1_response_gate_reason", "产品1响应体验理由"),
        ("answer2_response_gate", "产品2响应体验Gate"),
        ("answer2_response_gate_reason", "产品2响应体验理由"),
        ("answer1_safety_gate", "产品1安全稳定Gate"),
        ("answer1_safety_gate_reason", "产品1安全稳定理由"),
        ("answer2_safety_gate", "产品2安全稳定Gate"),
        ("answer2_safety_gate_reason", "产品2安全稳定理由"),
        ("answer3_response_gate", "产品3响应体验Gate"),
        ("answer3_response_gate_reason", "产品3响应体验理由"),
        ("answer3_safety_gate", "产品3安全稳定Gate"),
        ("answer3_safety_gate_reason", "产品3安全稳定理由"),
        ("understanding_applicable", "理解需求是否适用"),
        ("understanding_verification_status", "理解需求核验状态"),
        ("understanding_evidence", "理解需求证据"),
        ("understanding_primary_issue", "理解需求主要问题"),
        ("understanding_reason", "理解需求理由"),
        ("answer1_understanding_score", "产品1理解需求分"),
        ("answer2_understanding_score", "产品2理解需求分"),
        ("answer3_understanding_score", "产品3理解需求分"),
        ("understanding_rank_groups", "理解需求排名组"),
        ("accuracy_applicable", "内容准确性是否适用"),
        ("accuracy_verification_status", "内容准确性核验状态"),
        ("accuracy_evidence", "内容准确性证据"),
        ("accuracy_primary_issue", "内容准确性主要问题"),
        ("accuracy_reason", "内容准确性理由"),
        ("answer1_accuracy_score", "产品1内容准确性分"),
        ("answer2_accuracy_score", "产品2内容准确性分"),
        ("answer3_accuracy_score", "产品3内容准确性分"),
        ("accuracy_rank_groups", "内容准确性排名组（暂不汇总）"),
        ("service_closure_applicable", "服务闭环是否适用"),
        ("service_closure_verification_status", "服务闭环核验状态"),
        ("service_closure_evidence", "服务闭环证据"),
        ("service_closure_primary_issue", "服务闭环主要问题"),
        ("service_closure_reason", "服务闭环理由"),
        ("answer1_service_closure_score", "产品1服务闭环分"),
        ("answer2_service_closure_score", "产品2服务闭环分"),
        ("answer3_service_closure_score", "产品3服务闭环分"),
        ("service_closure_rank_groups", "服务闭环排名组"),
        ("scenario_fulfillment_applicable", "场景化满足是否适用"),
        ("scenario_fulfillment_verification_status", "场景化满足核验状态"),
        ("scenario_fulfillment_evidence", "场景化满足证据"),
        ("scenario_fulfillment_primary_issue", "场景化满足主要问题"),
        ("scenario_fulfillment_reason", "场景化满足理由"),
        ("answer1_scenario_fulfillment_score", "产品1场景化满足分"),
        ("answer2_scenario_fulfillment_score", "产品2场景化满足分"),
        ("answer3_scenario_fulfillment_score", "产品3场景化满足分"),
        ("scenario_fulfillment_rank_groups", "场景化满足排名组"),
        ("intuitive_efficiency_applicable", "直观高效是否适用"),
        ("intuitive_efficiency_verification_status", "直观高效核验状态"),
        ("intuitive_efficiency_evidence", "直观高效证据"),
        ("intuitive_efficiency_primary_issue", "直观高效主要问题"),
        ("intuitive_efficiency_reason", "直观高效理由"),
        ("answer1_intuitive_efficiency_score", "产品1直观高效分"),
        ("answer2_intuitive_efficiency_score", "产品2直观高效分"),
        ("answer3_intuitive_efficiency_score", "产品3直观高效分"),
        ("intuitive_efficiency_rank_groups", "直观高效排名组"),
        ("evidence_quality_applicable", "有理有据是否适用"),
        ("evidence_quality_verification_status", "有理有据核验状态"),
        ("evidence_quality_evidence", "有理有据证据"),
        ("evidence_quality_primary_issue", "有理有据主要问题"),
        ("evidence_quality_reason", "有理有据理由"),
        ("answer1_evidence_quality_score", "产品1有理有据分"),
        ("answer2_evidence_quality_score", "产品2有理有据分"),
        ("answer3_evidence_quality_score", "产品3有理有据分"),
        ("evidence_quality_rank_groups", "有理有据排名组"),
        ("guided_recommendation_applicable", "引导推荐是否适用"),
        ("guided_recommendation_verification_status", "引导推荐核验状态"),
        ("guided_recommendation_evidence", "引导推荐证据"),
        ("guided_recommendation_primary_issue", "引导推荐主要问题"),
        ("guided_recommendation_reason", "引导推荐理由"),
        ("answer1_guided_recommendation_score", "产品1引导推荐分"),
        ("answer2_guided_recommendation_score", "产品2引导推荐分"),
        ("answer3_guided_recommendation_score", "产品3引导推荐分"),
        ("guided_recommendation_rank_groups", "引导推荐排名组"),
        ("overall_ranking", "整体排名（当前版本暂不生成）"),
        ("evidence", "证据"),
        ("confidence", "置信度"),
        ("needs_human_review", "是否需要人工复核"),
        ("review_reasons", "人工复核原因"),
    ]
    _DISPLAY_MAP = {
        "relevance": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "safety": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "content_quality": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "need_closure": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "personalization": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "has_conflict": {"yes": "有冲突", "no": "无冲突", "unclear": "不清楚"},
        "answer1_input_status": {"complete": "完整", "partial": "不完整", "failed": "失败"},
        "answer2_input_status": {"complete": "完整", "partial": "不完整", "failed": "失败"},
        "answer3_input_status": {"complete": "完整", "partial": "不完整", "failed": "失败"},
        "answer1_response_gate": {"pass": "通过", "fail": "失败", "unclear": "不清楚"},
        "answer2_response_gate": {"pass": "通过", "fail": "失败", "unclear": "不清楚"},
        "answer3_response_gate": {"pass": "通过", "fail": "失败", "unclear": "不清楚"},
        "answer1_safety_gate": {"pass": "通过", "fail": "失败", "unclear": "不清楚"},
        "answer2_safety_gate": {"pass": "通过", "fail": "失败", "unclear": "不清楚"},
        "answer3_safety_gate": {"pass": "通过", "fail": "失败", "unclear": "不清楚"},
        "understanding_winner": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "readability_winner": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "accuracy_winner": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "decision_support_winner": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "closure_winner": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
        "overall_winner": {"answer1": "产品1更优", "answer2": "产品2更优", "tie": "平手"},
    }
    rows = []
    for r in results:
        row = {}
        for key, label in _COMPARE_COLUMNS:
            v = r.get(key)
            if v is None:
                row[label] = "N/A"
            elif key in _DISPLAY_MAP and v in _DISPLAY_MAP[key]:
                row[label] = _DISPLAY_MAP[key][v]
            else:
                row[label] = v if v != "" else ""
        rows.append(row)
    return rows


def result_export_row(mode: str, result: dict, index: int, items: list[dict]) -> dict:
    """单条 result → 与 xlsx/CSV「逐题结果」同名列、同转换的行。

    供 GET /api/eval/item/result 使用：键名与导出列完全一致，
    多余字段不返回；失败结果额外附加 error。item_id/query 缺失时
    按 index 从 items 回填，与导出的对齐逻辑保持一致。
    """
    row = dict(result)
    item = items[index] if 0 <= index < len(items) else {}
    if not row.get("item_id"):
        row["item_id"] = item.get("id") or f"q{index}"
    if not row.get("query"):
        row["query"] = item.get("query") or item.get("question") or ""
    export = (
        _visual_compare_export_rows([row])
        if mode == "compare"
        else _rich_content_export_rows([row])
    )[0]
    if result.get("error"):
        export["error"] = result["error"]
    return export


def _result_rows(results: list[dict]) -> list[dict]:
    """旧模式快照兜底：把维度展开成 分(维度_X) / 理由(理由_X) 两列。"""
    rows = []
    for r in results:
        row = dict(r)
        rubric = row.pop("rubric", {}) or {}
        reasons = row.pop("rubric_reasons", {}) or {}
        for dim, score in rubric.items():
            row[f"维度_{dim}"] = score
            row[f"理由_{dim}"] = reasons.get(dim, "")
        rows.append(row)
    return rows


def _flatten_dict(data: dict, skip_keys: set[str] | None = None) -> dict:
    skip_keys = skip_keys or set()
    return {k: v for k, v in data.items() if k not in skip_keys}


def rows_to_csv(rows: list[dict]) -> str:
    import csv
    from io import StringIO

    out = StringIO()
    keys = _headers(rows)
    writer = csv.DictWriter(out, fieldnames=keys)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in keys})
    return out.getvalue()


def write_frames_zip(
    snapshot: dict,
    destination: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    item_indexes: set[int] | None = None,
) -> Path:
    """将已有关键帧和映射清单打包到磁盘，避免大批量导出占用内存。"""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    items = snapshot.get("items") or []
    width = max(3, len(str(max(len(items), 1))))

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item_index, item in enumerate(items):
            if item_indexes is not None and item_index not in item_indexes:
                continue
            sequence = str(item_index + 1).zfill(width)
            raw_id = str(item.get("id") or f"q{item_index + 1}")
            safe_id = _safe_name(raw_id).strip("_")[:100] or f"q{item_index + 1}"
            item_dir = f"{sequence}_{safe_id}"
            for stream in _item_visual_streams(item):
                product_no = stream["product_no"]
                frames = stream["frames"]
                if stream["evidence_mode"] == "long_screenshot":
                    stream_dir = f"{item_dir}/product{product_no}"
                    meta = stream["screenshot_meta"]
                    evidence_paths = [("original", stream["original_path"])] + [
                        (f"part_{n:03d}", str(frame)) for n, frame in enumerate(frames, 1)
                    ]
                    for label, raw_path in evidence_paths:
                        path = Path(raw_path) if raw_path else None
                        exists = bool(path and path.is_file())
                        archive_path = f"{stream_dir}/{label}{path.suffix}" if exists else ""
                        if exists:
                            zf.write(path, archive_path)
                        manifest.append({
                            "dataset_index": item_index + 1, "id": raw_id, "product_no": product_no,
                            "evidence_mode": "long_screenshot", "image_role": label,
                            "image_path": archive_path, "status": "ok" if exists else "missing",
                            "split_status": meta.get("split_status", "未准备"),
                        })
                    zf.writestr(f"{stream_dir}/screenshot.json", json.dumps(meta, ensure_ascii=False, indent=2))
                    continue
                selected, metadata = (
                    _frame_metadata(frames[0].parent) if frames else ({}, {})
                )
                stream_dir = (
                    f"{item_dir}/product{product_no}"
                    if product_no is not None
                    else item_dir
                )
                base_manifest = {
                    "dataset_index": item_index + 1,
                    "id": raw_id,
                    "query": item.get("query") or item.get("question") or "",
                    "product_no": product_no,
                    "source_video_path": stream["source_video"],
                    "video_project_path": _project_relative_path(
                        stream["runtime_video"],
                        project_root,
                    ),
                }
                if not frames:
                    manifest.append({
                        **base_manifest,
                        "frame_index": None,
                        "frame_path": "",
                        "source_frame_project_path": "",
                        "timestamp": None,
                        "keep_reason": "",
                        "status": "missing",
                    })
                    continue

                for frame_index, frame in enumerate(frames, start=1):
                    info = selected.get(frame_index) or {}
                    archive_frame = f"{stream_dir}/{frame.name}"
                    exists = frame.is_file()
                    if exists:
                        zf.write(frame, archive_frame)
                    manifest.append({
                        **base_manifest,
                        "frame_index": frame_index,
                        "frame_path": archive_frame if exists else "",
                        "source_frame_project_path": _project_relative_path(
                            frame,
                            project_root,
                        ),
                        "timestamp": info.get("time"),
                        "source": info.get("source", ""),
                        "keep_reason": info.get("keep_reason", ""),
                        "status": "ok" if exists else "missing",
                    })

                if metadata:
                    exported_metadata = dict(metadata)
                    exported_metadata["video"] = _project_relative_path(
                        stream["runtime_video"],
                        project_root,
                    )
                    exported_metadata["product_no"] = product_no
                    zf.writestr(
                        f"{stream_dir}/keyframes.json",
                        json.dumps(exported_metadata, ensure_ascii=False, indent=2),
                    )

        manifest_text = "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in manifest
        )
        zf.writestr("manifest.jsonl", manifest_text)
    return target


def load_item_judge_calls(
    snapshot: dict,
    item_index: int,
    *,
    runs_dir: Path = RUNS_DIR,
    project_root: Path = PROJECT_ROOT,
    trace_paths: list[Path] | None = None,
) -> dict:
    """查找单条 case 对应的全部 judge_calls，并组装为可下载 JSON。

    以 task_id + item_index 为主键，item_id 仅作兼容校验，避免 query 重复时
    错配。候选日志包含当前环境配置的 trace 路径和 runs 下所有
    ``judge_calls*.jsonl``，因此加载历史任务后仍可导出。
    """
    items = snapshot.get("items") or []
    if item_index < 0 or item_index >= len(items):
        raise IndexError("item_index 超出数据集范围")
    item = items[item_index]
    task_id = str(snapshot.get("task_id") or "")
    session_name = str(snapshot.get("session_name") or "")
    item_id = str(item.get("id") or f"q{item_index}")

    candidates: list[Path] = []
    if trace_paths is not None:
        candidates.extend(Path(path) for path in trace_paths)
    else:
        configured = str(os.getenv("AUTO_EVAL_JUDGE_TRACE") or "").strip()
        if configured:
            configured_path = Path(configured).expanduser()
            if not configured_path.is_absolute():
                configured_path = project_root / configured_path
            candidates.append(configured_path)
        candidates.extend(sorted(runs_dir.rglob("judge_calls*.jsonl")))

    unique_candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved not in seen:
            seen.add(resolved)
            unique_candidates.append(candidate)

    records: list[dict] = []
    task_needle = f'"{task_id}"' if task_id else ""
    for path in unique_candidates:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig") as file:
                for line in file:
                    if task_needle and task_needle not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if task_id and str(record.get("task_id") or "") != task_id:
                        continue
                    if not task_id and session_name:
                        if str(record.get("session_name") or "") != session_name:
                            continue
                    try:
                        record_index = int(record.get("item_index"))
                    except (TypeError, ValueError):
                        record_index = -1
                    if record_index != item_index:
                        continue
                    record_item_id = str(record.get("item_id") or "")
                    if record_item_id and record_item_id != item_id:
                        continue
                    exported = dict(record)
                    exported["_trace_file"] = _project_relative_path(
                        path,
                        project_root,
                    ) or path.name
                    records.append(exported)
        except OSError:
            continue

    def record_sort_key(row: dict) -> tuple[str, str, int]:
        try:
            round_index = int(row.get("round") or 0)
        except (TypeError, ValueError):
            round_index = 0
        return (
            str(row.get("ts") or ""),
            str(row.get("judge") or ""),
            round_index,
        )

    records.sort(key=record_sort_key)
    return {
        "task_id": snapshot.get("task_id"),
        "session_name": snapshot.get("session_name"),
        "dataset_name": snapshot.get("dataset_name") or "",
        "dataset_index": item_index + 1,
        "item_index": item_index,
        "item_id": item_id,
        "query": item.get("query") or item.get("question") or "",
        "judge_call_count": len(records),
        "judge_calls": records,
    }


_PRODUCT3_XLSX_COLUMN_PREFIXES = (
    "产品3", "answer3", "context3", "video3", "screenshot3", "frames3", "duration3",
)


def _compare_snapshot_uses_product3(snapshot: dict) -> bool:
    """按任务输入/结果判断 XLSX 是否需要保留产品3列。"""
    for record in [
        *(snapshot.get("items") or []),
        *(snapshot.get("results") or []),
    ]:
        if record.get("product_count") == 3:
            return True
        source = record.get("source_data")
        candidates = [record, source] if isinstance(source, dict) else [record]
        if any(
            candidate.get(field) not in (None, "", [])
            for candidate in candidates
            for field in ("answer3", "context3", "video3", "screenshot3", "frames3", "duration3")
        ):
            return True
    return False


def _original_screenshot_rows(snapshot: dict, images: WpsCellImages) -> list[dict]:
    """一条输入一行、每个产品一列；原图缺失时不以切片或其他产品代替。"""
    items = snapshot.get("items") or []
    streams_by_item = [_item_visual_streams(item) for item in items]
    screenshot_streams = [
        stream for streams in streams_by_item for stream in streams
        if stream["evidence_mode"] == "long_screenshot"
    ]
    if not screenshot_streams:
        return []
    count = max(stream["product_no"] for stream in screenshot_streams)
    rows = []
    for index, (item, streams) in enumerate(zip(items, streams_by_item)):
        row = {
            "数据集序号": index + 1, "id": item.get("id") or f"q{index}",
            "query": item.get("query") or item.get("question") or "",
            **{f"产品{n}原图": "" for n in range(1, count + 1)},
        }
        for stream in streams:
            product_no = stream["product_no"]
            if product_no not in range(1, count + 1):
                continue
            key = f"产品{product_no}原图"
            if stream["evidence_mode"] != "long_screenshot":
                row[key] = "录屏模式，无原始长截图"
                continue
            try:
                row[key] = images.add(
                    stream["original_path"], stream["screenshot_meta"].get("original_sha256"),
                )
            except OriginalImageError as exc:
                row[key] = str(exc)
        rows.append(row)
    return rows


def build_xlsx(snapshot: dict) -> bytes:
    """生成评分数据及 WPS 原始长截图页；图片按原字节嵌入，不影响正式评分列。"""
    buf = BytesIO()
    write_xlsx(snapshot, buf)
    return buf.getvalue()


def write_xlsx(snapshot: dict, destination) -> None:
    """直接写入文件/二进制流，线上下载不把完整工作簿缓存在内存中。"""
    with _xlsx_slot:
        _write_xlsx(snapshot, destination)


def _write_xlsx(snapshot: dict, destination) -> None:
    sheets = {name: rows for name, rows in export_rows(snapshot).items() if rows}
    if snapshot.get("mode") == "compare" and not _compare_snapshot_uses_product3(snapshot):
        for sheet_name in ("数据集明细", "逐题结果"):
            if sheet_name in sheets:
                sheets[sheet_name] = [
                    {
                        key: value
                        for key, value in row.items()
                        if not str(key).startswith(_PRODUCT3_XLSX_COLUMN_PREFIXES)
                    }
                    for row in sheets[sheet_name]
                ]
    if not sheets:
        sheets = {"逐题结果": []}

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        images = WpsCellImages(zf)
        screenshot_rows = _original_screenshot_rows(snapshot, images)
        if screenshot_rows:
            sheets["原始长截图"] = screenshot_rows
        images.write_parts()
        zf.writestr("[Content_Types].xml", _content_types(len(sheets), images.content_types_xml()))
        zf.writestr("_rels/.rels", _root_rels())
        zf.writestr("xl/workbook.xml", _workbook_xml(list(sheets), bool(screenshot_rows)))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets), bool(images.images)))
        zf.writestr("xl/styles.xml", _styles_xml(bool(screenshot_rows)))
        for i, (name, rows) in enumerate(sheets.items(), start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(rows, picture_sheet=name == "原始长截图"))


def _headers(rows: list[dict]) -> list[str]:
    keys: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    return keys


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _content_types(sheet_count: int, image_types: str = "") -> str:
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}{image_types}</Types>"
    )


def _root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


def _workbook_xml(names: list[str], picture_sheet: bool = False) -> str:
    sheets = "".join(
        f'<sheet name="{escape(_sheet_name(name))}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(names, start=1)
    )
    views = '<bookViews><workbookView/></bookViews>' if picture_sheet else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"{views}<sheets>{sheets}</sheets></workbook>"
    )


def _sheet_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\*:/\\?]", "_", name)
    return cleaned[:31] or "Sheet"


def _workbook_rels(sheet_count: int, has_cell_images: bool = False) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, sheet_count + 1)
    )
    rels += (
        f'<Relationship Id="rId{sheet_count + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    if has_cell_images:
        rels += (
            f'<Relationship Id="rId{sheet_count + 2}" Type="{CELL_IMAGE_REL}" '
            'Target="cellimages.xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels}</Relationships>"
    )


def _styles_xml(picture_sheet: bool = False) -> str:
    picture_style = (
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
    ) if picture_sheet else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        f'<cellXfs count="{3 if picture_sheet else 2}"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/>'
        f'{picture_style}</cellXfs>'
        '</styleSheet>'
    )


def _sheet_xml(rows: list[dict], *, picture_sheet: bool = False) -> str:
    headers = _headers(rows)
    table = [headers] + [[row.get(h) for h in headers] for row in rows]
    rows_xml = []
    for r_idx, row in enumerate(table, start=1):
        cells = []
        for c_idx, value in enumerate(row, start=1):
            ref = f"{_col(c_idx)}{r_idx}"
            style = ' s="1"' if r_idx == 1 else (' s="2"' if picture_sheet else "")
            if isinstance(value, CellImage):
                # 仅可信 CellImage 生成公式；Query 等输入仍按原来的纯文本方式导出。
                formula = escape(value.formula)
                cells.append(f'<c r="{ref}" t="str"{style}><f>_xlfn.{formula}</f><v>={formula}</v></c>')
            elif (
                r_idx > 1
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            ):
                cells.append(f'<c r="{ref}"{style}><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"{style}><is><t>{escape(_cell(value))}</t></is></c>')
        height = ' ht="240" customHeight="1"' if picture_sheet and r_idx > 1 else ""
        rows_xml.append(f'<row r="{r_idx}"{height}>{"".join(cells)}</row>')
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{36 if picture_sheet and h.startswith("产品") else _width(h)}" customWidth="1"/>'
        for i, h in enumerate(headers, start=1)
    )
    views = (
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane xSplit="3" ySplit="1" topLeftCell="D2" activePane="bottomRight" state="frozen"/>'
        '<selection pane="bottomRight" activeCell="D2" sqref="D2"/>'
        '</sheetView></sheetViews>'
    ) if picture_sheet else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{views}<cols>{cols}</cols><sheetData>{''.join(rows_xml)}</sheetData>"
        "</worksheet>"
    )


def _col(idx: int) -> str:
    out = ""
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _width(header: str) -> int:
    if header in {"query", "answer", "generated_answer", "rationale", "理由", "options"}:
        return 42
    if header.startswith("理由_"):
        return 30
    if header.startswith("维度_"):
        return 14
    return 18
