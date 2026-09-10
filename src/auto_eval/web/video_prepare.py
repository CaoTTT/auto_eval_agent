"""视频视觉评估的路径校验、缓存与关键帧准备。"""
from __future__ import annotations

import json
import math
import os
import re
from numbers import Real
from pathlib import Path
from typing import Callable

from ..media import (
    KEYFRAME_ALGORITHM_VERSION,
    KeyframeConfig,
    extract_scene_keyframes,
    probe_duration,
)
from ..config import VisualModeProfile
from ..paths import PROJECT_ROOT, RUNS_DIR
from ..preparation import check_preparation


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_TASK_TIME_FIELDS = ("task_start_time", "task_end_time")


def _safe_name(value: str, fallback: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", value.strip())
    return safe or fallback


def operation_video_roots(base_dir: Path = PROJECT_ROOT) -> list[Path]:
    """返回批量清单允许读取的视频根目录。"""
    roots = [base_dir.resolve()]
    for raw in os.getenv("OPERATION_VIDEO_ROOTS", "").split(os.pathsep):
        if raw.strip():
            root = Path(raw.strip()).expanduser()
            if not root.is_absolute():
                root = base_dir / root
            roots.append(root.resolve())
    return roots


def resolve_operation_video_path(
    raw_path: str,
    *,
    base_dir: Path = PROJECT_ROOT,
) -> Path:
    """解析本地视频路径，并阻止读取未授权目录。"""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    candidate = candidate.resolve()
    if not any(candidate.is_relative_to(root) for root in operation_video_roots(base_dir)):
        raise ValueError(
            "视频路径不在允许目录中；相对路径请以项目根目录为基准，"
            "外部目录需通过 OPERATION_VIDEO_ROOTS 配置"
        )
    if not candidate.is_file():
        raise ValueError(f"视频文件不存在：{raw_path}")
    if candidate.suffix.lower() not in VIDEO_EXTENSIONS:
        supported = ", ".join(sorted(VIDEO_EXTENSIONS))
        raise ValueError(f"不支持的视频格式 {candidate.suffix or '(无扩展名)'}；支持：{supported}")
    return candidate


def _cached_frames(
    frame_dir: Path,
    cache_key: str = KEYFRAME_ALGORITHM_VERSION,
) -> list[Path]:
    marker = frame_dir / ".complete"
    if not marker.exists():
        return []
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("cache_key") != cache_key:
            return []
        expected = int(payload["frame_count"])
        frames = sorted(frame_dir.glob("kf_*.jpg"))
        return frames if expected > 0 and len(frames) == expected else []
    except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        return []


def _extract_frames(
    video_path: Path,
    frame_dir: Path,
    *,
    extract_fn: Callable = extract_scene_keyframes,
    cache_key: str = KEYFRAME_ALGORITHM_VERSION,
    extract_kwargs: dict | None = None,
) -> list[Path]:
    check_preparation()
    frames = _cached_frames(frame_dir, cache_key)
    if frames:
        return frames
    frame_dir.mkdir(parents=True, exist_ok=True)
    for stale in frame_dir.glob("kf_*.jpg"):
        stale.unlink(missing_ok=True)
    (frame_dir / ".complete").unlink(missing_ok=True)
    (frame_dir / "keyframes.json").unlink(missing_ok=True)
    frames = list(extract_fn(video_path, frame_dir, **(extract_kwargs or {})))
    check_preparation()  # Never mark an interrupted extraction as a complete cache.
    if frames:
        (frame_dir / ".complete").write_text(
            json.dumps(
                {
                    "cache_key": cache_key,
                    "frame_count": len(frames),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return frames


def _rich_content_timing(
    item: dict,
    duration: float,
    profile: VisualModeProfile,
    video_path: str = "",
) -> tuple[dict, str]:
    """校验富内容视频时间窗，并构造专用抽帧配置和缓存键。

    缓存键包含视频路径：同一 frame_dir（同 session/index/id）换视频重评时，
    旧 ``.complete`` 标记因 key 不匹配自动失效重抽，避免评到旧帧。
    """
    supplied: dict[str, float] = {}
    for field in _TASK_TIME_FIELDS:
        value = item.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{field} 必须是有限数字（单位：秒）")
        if normalized < 0:
            raise ValueError(f"{field} 不能小于 0")
        if normalized > duration:
            raise ValueError(f"{field}={normalized:g} 超出视频时长 {duration:g} 秒")
        supplied[field] = normalized

    extraction = profile.extraction
    start = supplied.get("task_start_time", extraction.default_start_time)
    end = supplied.get("task_end_time")
    if end is not None and end <= start:
        raise ValueError("task_end_time 必须大于 task_start_time")

    config = KeyframeConfig(
        task_start_time=start,
        task_end_time=end,
        max_frames=extraction.max_frames,
        sample_fps=extraction.sample_fps,
        scene_threshold=extraction.scene_threshold,
        scene_min_gap_s=extraction.scene_min_gap_s,
        state_layout_threshold=extraction.state_layout_threshold,
        stable_min_duration_s=extraction.stable_min_duration_s,
        max_edge=extraction.max_edge,
        # 问答视频通常不会返回操作助手外壳，禁用任务类（录屏）的自动结束点推断。
        auto_task_end_confidence_threshold=2.0,
        final_dedup_rms_threshold=0.004,
        final_dedup_changed_fraction_threshold=0.004,
        # 垂域视觉（问答视频）不需要开头/结尾受保护采样。
        protected_sample_interval=0.0,
    )
    cache_payload = {
        "algorithm_version": extraction.algorithm_version,
        "video": video_path,
        "config": {
            "task_start_time": start,
            "task_end_time": end,
            "max_frames": extraction.max_frames,
            "sample_fps": extraction.sample_fps,
            "scene_threshold": extraction.scene_threshold,
            "scene_min_gap_s": extraction.scene_min_gap_s,
            "state_layout_threshold": extraction.state_layout_threshold,
            "stable_min_duration_s": extraction.stable_min_duration_s,
            "max_edge": extraction.max_edge,
        },
    }
    cache_key = json.dumps(
        cache_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "config": config,
        "algorithm_version": extraction.algorithm_version,
    }, cache_key


def _prepared_item(item: dict, video_path: Path, frames: list[Path], duration: float) -> dict:
    prepared = dict(item)
    prepared.update({
        "video_path": str(video_path),
        "video_name": video_path.name,
        "media": [str(video_path)],
        "frames": [str(frame) for frame in frames],
        "frame_count": len(frames),
        "duration": round(duration, 2),
    })
    return prepared


def prepare_session_rich_content_item(
    item: dict,
    *,
    profile: VisualModeProfile,
    session_name: str,
    item_index: int,
    total_items: int,
    base_dir: Path = PROJECT_ROOT,
    runs_dir: Path = RUNS_DIR,
    probe_fn: Callable = probe_duration,
    extract_fn: Callable = extract_scene_keyframes,
) -> dict:
    """按 Web 会话准备垂域视觉评测关键帧。"""
    raw_path = str(item.get("video_path") or "").strip()
    if not raw_path:
        media = item.get("media") or []
        raw_path = str(media[0]).strip() if media else ""
    if not raw_path:
        raise ValueError("缺少 video_path")
    video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
    duration = float(probe_fn(video_path))
    if duration <= 0:
        raise ValueError(f"无法读取视频或视频时长为 0：{raw_path}")
    extract_kwargs, cache_key = _rich_content_timing(
        item, duration, profile, video_path=str(video_path)
    )

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(
        str(item.get("id") or f"q{item_index + 1}"),
        f"q{item_index + 1}",
    )
    safe_session = _safe_name(session_name, "rich_content_session")
    frame_dir = (
        runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    )
    frames = _extract_frames(
        video_path,
        frame_dir,
        extract_fn=extract_fn,
        cache_key=cache_key,
        extract_kwargs=extract_kwargs,
    )
    if not frames:
        raise ValueError(f"视频抽帧失败：{raw_path}")
    return _prepared_item(item, video_path, frames, duration)


def prepare_session_visual_compare_item(
    item: dict,
    *,
    profile,
    session_name: str,
    item_index: int,
    total_items: int,
    base_dir = PROJECT_ROOT,
    runs_dir = RUNS_DIR,
    probe_fn = probe_duration,
    extract_fn = extract_scene_keyframes,
) -> dict:
    """按 Web 会话准备双/三产品视觉对比关键帧。"""
    product_count = item.get("product_count")
    if product_count is None:
        product_count = 3 if str(item.get("video3") or "").strip() else 2
    if product_count not in (2, 3):
        raise ValueError("product_count 只能是 2 或 3")

    width = max(3, len(str(max(total_items, 1))))
    sequence = str(item_index + 1).zfill(width)
    item_name = _safe_name(
        str(item.get("id") or f"q{item_index + 1}"),
        f"q{item_index + 1}",
    )
    safe_session = _safe_name(session_name, "visual_compare_session")
    base_frame_dir = (
        runs_dir / "videos" / "imported" / safe_session / f"{sequence}_{item_name}"
    )

    prepared = dict(item)
    prepared["product_count"] = product_count
    media: list[str] = []
    total_frame_count = 0
    first_video_name = ""
    first_duration = 0.0

    for product_no in range(1, product_count + 1):
        check_preparation()
        raw_path = str(item.get(f"video{product_no}") or "").strip()
        if not raw_path:
            raise ValueError(f"缺少 video{product_no}")
        video_path = resolve_operation_video_path(raw_path, base_dir=base_dir)
        duration = float(probe_fn(video_path))
        if duration <= 0:
            raise ValueError(
                f"无法读取视频{product_no}或视频时长为0：{raw_path}"
            )
        extract_kwargs, cache_key = _rich_content_timing(
            item,
            duration,
            profile,
            video_path=str(video_path),
        )
        frame_dir = base_frame_dir / f"video{product_no}"
        frames = _extract_frames(
            video_path,
            frame_dir,
            extract_fn=extract_fn,
            cache_key=cache_key,
            extract_kwargs=extract_kwargs,
        )
        if not frames:
            raise ValueError(f"视频{product_no}抽帧失败：{raw_path}")

        media.append(str(video_path))
        total_frame_count += len(frames)
        first_video_name = first_video_name or video_path.name
        first_duration = first_duration or duration
        prepared[f"video{product_no}_path"] = str(video_path)
        prepared[f"frames{product_no}"] = [str(frame) for frame in frames]
        prepared[f"duration{product_no}"] = round(duration, 2)

    prepared.update({
        "video_name": first_video_name,
        "media": media,
        "frame_count": total_frame_count,
        "duration": round(first_duration, 2),
    })
    return prepared


def prepare_session_long_screenshot_item(
    item: dict,
    *,
    profile: VisualModeProfile,
    session_name: str,
    item_index: int,
    total_items: int,
    base_dir: Path = PROJECT_ROOT,
    runs_dir: Path = RUNS_DIR,
) -> dict:
    """复用现有允许目录和任务命名；不进入视频探测或抽帧。"""
    from ..long_screenshot import prepare_long_screenshot
    from .parse_input import compare_evidence_mode

    count, mode = compare_evidence_mode(item)
    if mode != "long_screenshot":
        raise ValueError("长截图准备只接受 screenshotN")
    sequence = str(item_index + 1).zfill(max(3, len(str(max(total_items, 1)))))
    item_name = _safe_name(str(item.get("id") or f"q{item_index + 1}"), f"q{item_index + 1}")
    root = runs_dir / "screenshots" / _safe_name(session_name, "compare") / f"{sequence}_{item_name}"
    prepared = dict(item)
    prepared.update(product_count=count, evidence_mode=mode, frame_count=0, media=[])
    for product_no in range(1, count + 1):
        check_preparation()
        path = Path(item[f"screenshot{product_no}"]).expanduser()
        path = (path if path.is_absolute() else base_dir / path).resolve()
        if not any(path.is_relative_to(allowed) for allowed in operation_video_roots(base_dir)):
            raise ValueError("截图路径不在允许目录中；外部目录需通过 OPERATION_VIDEO_ROOTS 配置")
        meta = prepare_long_screenshot(path, root / f"product{product_no}", profile.long_screenshot)
        # 运行时和快照均保存相对项目根目录的路径，不依赖启动时工作目录。
        for record, key in [(meta, "original_path"), *[(part, "path") for part in meta["slices"]]]:
            record[key] = Path(os.path.relpath(record[key], base_dir)).as_posix()
        prepared[f"screenshot_meta{product_no}"] = meta
        prepared[f"screenshot{product_no}"] = meta["original_path"]
        prepared[f"frames{product_no}"] = [part["path"] for part in meta["slices"]]
        prepared["frame_count"] += meta["split_count"]
        prepared["media"].append(meta["original_path"])
    return prepared
