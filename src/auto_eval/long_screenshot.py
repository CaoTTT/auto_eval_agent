"""原字节直传与全宽、无重叠长截图切片。仅边界检测使用灰度预览。"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .config import LongScreenshotConfig


ALGORITHM_VERSION = "long-screenshot-v1"
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
RISKY_REVIEW_REASON = "长截图存在高风险切分边界，可能影响局部证据识别"
# 无 OCR 的保守几何代理，不声称能够识别文字语义、标题或价格。
RISK_WEIGHTS = {"text": 1000, "component": 150, "edge": 50, "blank": 50}


class ContextBudgetExceeded(ValueError):
    code = "context_budget_exceeded"


def data_url_size(file_bytes: int, mime: str = "image/png") -> int:
    return len(f"data:{mime};base64,") + 4 * ((file_bytes + 2) // 3)


def image_tokens(width: int, height: int) -> int:
    return math.ceil(width / 32) * math.ceil(height / 32)


def _read_image(path: Path) -> tuple[bytes, Image.Image, str]:
    raw = path.read_bytes()
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in MIME_TYPES or getattr(source, "n_frames", 1) != 1:
            raise ValueError("长截图只支持静态 PNG/JPEG/WebP")
        mime = MIME_TYPES[source.format]
        source.load()  # 完整解码，损坏图片不得发送给裁判。
        image = source.copy()
    return raw, image, mime


def _fits(width: int, height: int, encoded_bytes: int, cfg: LongScreenshotConfig) -> bool:
    return (
        min(width, height) >= cfg.min_edge
        and max(width, height) / min(width, height) <= cfg.max_aspect_ratio
        and width * height <= cfg.max_pixels
        and encoded_bytes < cfg.max_data_url_bytes
    )


def encode_original_image(path: Path, cfg: LongScreenshotConfig, expected_sha256: str | None = None) -> str:
    """原图及已准备 PNG 共用此出口：校验后仅 Base64，不 save/resize/转码。"""
    raw, image, mime = _read_image(path)
    try:
        if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("视觉证据文件在预处理后发生变化，请重新准备")
        if not _fits(*image.size, data_url_size(len(raw), mime), cfg):
            raise ValueError("视觉证据图片超过单图限制，请重新准备长截图")
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
    finally:
        image.close()


def boundary_signals(image: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """逐行检测，不降低垂直分辨率，避免把细字行缩掉后误认作安全空白。

    safe=连续背景空白；fallback=低纹理但非页面背景；risky=纹理/文字或
    连通前景跨越边界。每个整数行都是候选，不因采样遗漏可行最少张数。
    """
    preview = image.convert("RGB")
    if preview.width > 512:
        preview = preview.resize((512, preview.height), Image.Resampling.BOX)
    gray = np.asarray(preview.convert("L"), dtype=np.float32)
    preview.close()
    height = image.height
    # 页面外缘的众数作为背景，兼容暗色主题；不把彩色组件内部当成空白。
    edges = np.concatenate((gray[:, :2].ravel(), gray[:, -2:].ravel()))
    background = float(np.bincount(edges.astype(np.uint8)).argmax())
    foreground = np.abs(gray - background) > 14
    horizontal = np.abs(np.diff(gray, axis=1)) > 18
    density = horizontal.mean(axis=1)
    texture = gray.std(axis=1)
    blank = (foreground.mean(axis=1) < 0.005) & (texture < 3)
    statuses = np.full(height + 1, "risky", dtype="<U8")
    risks = np.full(height + 1, RISK_WEIGHTS["text"], dtype=np.float64)
    for y in range(1, height):
        lo, hi = max(0, y - 2), min(height, y + 2)
        edge = float(density[lo:hi].max())
        crossed = foreground[y - 1] & foreground[y]
        # 多段窄前景或显著纹理跨行，保守视为文字/关键组件。
        transitions = np.count_nonzero(np.diff(crossed.astype(np.int8)))
        if blank[lo:hi].all():
            statuses[y] = "safe"
            risks[y] = 0
        elif (
            transitions <= 2 and crossed.mean() > 0.1
            and horizontal[lo:hi].sum(axis=1).max() <= 2
            and gray[lo:hi, :][:, crossed].std() < 12
        ):
            statuses[y] = "fallback"
            risks[y] = RISK_WEIGHTS["component"] + RISK_WEIGHTS["edge"] * edge
        else:
            risks[y] = RISK_WEIGHTS["text"] + RISK_WEIGHTS["edge"] * edge
    # 同一空白带中心风险最低；更宽的空白带优先，但奖励有界。
    safe = statuses == "safe"
    changes = np.diff(np.r_[False, safe, False].astype(np.int8))
    for start, end in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
        for y in range(start, end):
            clearance = min(y - start + 1, end - y)
            risks[y] = -RISK_WEIGHTS["blank"] * min(clearance, 32) / 32
    risks[0] = risks[height] = 0
    return statuses, risks


def _optimal_path(
    height: int, min_height: int, max_height: int, risks: np.ndarray,
    blocked: dict[int, set[int]],
) -> list[int] | None:
    """整数行 DAG 的精确 DP。编码失败边按需删除，再求全局最优路径。

    字典序目标：张数、风险和、平方高度和（固定张数时等价于方差）、
    靠后的边界。先忽略未知编码边，仅编码所选路径；不假设 PNG 大小单调。
    """
    counts = np.full(height + 1, height + 1, dtype=np.int64)
    totals = np.full(height + 1, np.inf)
    balance = np.full(height + 1, np.inf)
    previous = np.full(height + 1, -1, dtype=np.int64)
    counts[0], totals[0], balance[0] = 0, 0, 0

    def boundary_order(index: int) -> tuple[int, ...]:
        path = []
        while index > 0:
            path.append(index)
            index = int(previous[index])
        return tuple(reversed(path))

    for end in range(min_height, height + 1):
        low, high = max(0, end - max_height), end - min_height + 1
        if high <= low:
            continue
        starts = np.arange(low, high)
        valid = counts[low:high] <= height
        for start in blocked.get(end, ()):
            if low <= start < high:
                valid[start - low] = False
        starts = starts[valid]
        if not len(starts):
            continue
        starts = starts[counts[starts] == counts[starts].min()]
        starts = starts[totals[starts] == totals[starts].min()]
        costs = balance[starts] + (end - starts).astype(np.float64) ** 2
        tied = starts[costs == costs.min()]
        start = int(tied[0]) if len(tied) == 1 else int(max(tied, key=boundary_order))
        counts[end] = counts[start] + 1
        totals[end] = totals[start] + risks[end]
        balance[end] = balance[start] + (end - start) ** 2
        previous[end] = start
    if previous[height] < 0:
        return None
    path = [height]
    while path[-1]:
        path.append(int(previous[path[-1]]))
    return path[::-1]


def _png_bytes(image: Image.Image, start: int, end: int) -> bytes:
    with image.crop((0, start, image.width, end)) as part:
        buffer = io.BytesIO()
        part.save(buffer, format="PNG")
        return buffer.getvalue()


def prepare_long_screenshot(
    path: Path, output_dir: Path, cfg: LongScreenshotConfig,
) -> dict:
    """返回不含 Base64 的元数据；original 和 slices 互斥用于模型输入。"""
    started = time.perf_counter()
    raw, image, mime = _read_image(path)
    try:
        width, height = image.size
        if min(width, height) < cfg.min_edge:
            raise ValueError("长截图最小边必须大于 10 px")
        encoded_size = data_url_size(len(raw), mime)
        meta = {
            "evidence_mode": "long_screenshot", "algorithm_version": ALGORITHM_VERSION,
            "original_path": str(path), "original_sha256": hashlib.sha256(raw).hexdigest(),
            "original_width": width, "original_height": height,
            "original_pixels": width * height, "original_file_bytes": len(raw),
            "original_data_url_bytes": encoded_size, "original_mime": mime,
            "limits": cfg.model_dump(),
            "has_overlap": False, "overlap_pixels": 0, "boundaries": [],
        }
        if _fits(width, height, encoded_size, cfg):
            paths, cuts, sizes = [path], [0, height], [encoded_size]
            meta["split_status"] = "original"
        else:
            # PNG 不支持 CMYK，不能为了切片静默转换原始颜色通道。
            if image.mode == "CMYK":
                raise ValueError("超限 CMYK JPEG 无法无损切成 PNG；请提供 RGB JPEG 或 PNG 长截图")
            min_height = max(cfg.min_edge, math.ceil(width / cfg.max_aspect_ratio))
            max_height = min(cfg.max_pixels // width, math.floor(width * cfg.max_aspect_ratio))
            if max_height < min_height:
                raise ValueError("原始宽度无法在单图限制内进行全宽水平切片")
            statuses, risks = boundary_signals(image)
            blocked: dict[int, set[int]] = {}
            valid_sizes: dict[tuple[int, int], int] = {}
            while True:
                cuts = _optimal_path(height, min_height, max_height, risks, blocked)
                if cuts is None:
                    raise ValueError("原始宽度及编码大小限制下不存在合法的连续水平切片")
                invalid = False
                for start, end in zip(cuts, cuts[1:]):
                    if (start, end) in valid_sizes:
                        continue
                    size = data_url_size(len(_png_bytes(image, start, end)))
                    if size >= cfg.max_data_url_bytes:
                        blocked.setdefault(end, set()).add(start)
                        invalid = True
                    else:
                        valid_sizes[start, end] = size
                if not invalid:
                    break
            output_dir.mkdir(parents=True, exist_ok=True)
            count = len(cuts) - 1
            paths, sizes = [], []
            for part_no, (start, end) in enumerate(zip(cuts, cuts[1:]), 1):
                part_path = output_dir / f"part_{part_no:03d}_of_{count:03d}.png"
                part_path.write_bytes(_png_bytes(image, start, end))
                paths.append(part_path)
                sizes.append(valid_sizes[start, end])
            for y in cuts[1:-1]:
                status = str(statuses[y])
                meta["boundaries"].append({
                    "y": y, "status": status, "risk_score": float(risks[y]),
                    "reason": {"safe": "连续背景空白行间", "fallback": "低纹理组件背景，无明显文字",
                               "risky": "疑似文字或关键组件跨越边界"}[status],
                    "crossed_text": status == "risky",
                    "crossed_elements": [] if status == "safe" else [
                        "card_background" if status == "fallback" else "text_or_component"],
                })
            status_set = {b["status"] for b in meta["boundaries"]}
            meta["split_status"] = "risky" if "risky" in status_set else (
                "fallback" if "fallback" in status_set else "safe")
        meta["split_count"] = len(paths)
        meta["slices"] = [
            {"path": str(part_path), "start_y": start, "end_y": end,
             "width": width, "height": end - start, "pixels": width * (end - start),
             "data_url_bytes": size, "sha256": meta["original_sha256"] if meta["split_status"] == "original" else hashlib.sha256(part_path.read_bytes()).hexdigest()}
            for part_path, start, end, size in zip(paths, cuts, cuts[1:], sizes)
        ]
        meta["estimated_image_tokens"] = sum(image_tokens(width, b - a) for a, b in zip(cuts, cuts[1:]))
        meta["prepare_latency_ms"] = int((time.perf_counter() - started) * 1000)
        return meta
    finally:
        image.close()


def check_context_budget(system: str, texts: list[str], metas: list[dict], cfg: LongScreenshotConfig) -> None:
    # UTF-8 字节数为保守文本 token 上界；每图留额外封装余量。
    visual = sum(int(meta["estimated_image_tokens"]) + 256 * len(meta["slices"]) for meta in metas)
    text = len(system.encode("utf-8")) + sum(len(t.encode("utf-8")) for t in texts) + 256
    if visual + text + cfg.output_reserve_tokens >= min(cfg.max_input_tokens, cfg.context_window):
        raise ContextBudgetExceeded(
            f"context_budget_exceeded：视觉估算 {visual}，文本保守估算 {text}，"
            f"输出预留 {cfg.output_reserve_tokens}；保留全部证据并转人工复核"
        )
