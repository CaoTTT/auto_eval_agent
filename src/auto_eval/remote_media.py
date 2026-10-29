"""Download HTTP(S) media used as evaluation inputs."""
from __future__ import annotations

import math
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .paths import RUNS_DIR
from .preparation import check_preparation


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
TEMPORARY_DOWNLOAD_PATHS_FIELD = "_temporary_download_paths"

_VIDEO_CONTENT_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}
_IMAGE_CONTENT_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def is_remote_url(value: str) -> bool:
    """Return True only for absolute HTTP(S) URLs."""
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def remote_filename(url: str, fallback: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return name or fallback


def _safe_name(value: str, fallback: str) -> str:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", value.strip())
    return safe or fallback


def _remote_suffix(
    url: str,
    content_type: str,
    *,
    extensions: set[str],
    content_suffixes: dict[str, str],
    fallback: str,
) -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower()
    if suffix in extensions:
        return suffix
    media_type = content_type.split(";", 1)[0].strip().lower()
    return content_suffixes.get(media_type, fallback)


def _download_timeout(timeout_s: float | None) -> float:
    if timeout_s is None:
        try:
            timeout_s = float(os.getenv("AUTO_EVAL_MEDIA_DOWNLOAD_TIMEOUT_S", "120"))
        except ValueError:
            timeout_s = 120.0
    return timeout_s if math.isfinite(timeout_s) and timeout_s > 0 else 120.0


def _image_suffix_from_header(path: Path) -> str:
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return ""


def _download_remote_media(
    url: str,
    output_dir: Path,
    *,
    kind: str,
    extensions: set[str],
    content_suffixes: dict[str, str],
    fallback_suffix: str,
    fallback_name: str,
    timeout_s: float | None,
) -> Path:
    if not is_remote_url(url):
        raise ValueError(f"不支持的{kind}网络地址：{url}")
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout_value = _download_timeout(timeout_s)
    deadline = time.monotonic() + timeout_value

    def check_download() -> None:
        check_preparation()
        if time.monotonic() >= deadline:
            raise TimeoutError(f"网络{kind}下载超时")

    timeout = httpx.Timeout(
        connect=min(30.0, timeout_value),
        read=timeout_value,
        write=timeout_value,
        pool=min(30.0, timeout_value),
    )
    part_path: Path | None = None
    try:
        check_download()
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=timeout,
        ) as response:
            check_download()
            response.raise_for_status()
            suffix = _remote_suffix(
                str(response.url),
                response.headers.get("content-type", ""),
                extensions=extensions,
                content_suffixes=content_suffixes,
                fallback=fallback_suffix,
            )
            stem = _safe_name(Path(remote_filename(url, fallback_name)).stem, kind)[:80]
            final_path = output_dir / f"{stem}_{uuid.uuid4().hex}{suffix}"
            part_path = output_dir / f".{final_path.name}.part"
            size = 0
            with part_path.open("wb") as output:
                for chunk in response.iter_bytes():
                    check_download()
                    if not chunk:
                        continue
                    output.write(chunk)
                    size += len(chunk)
            if size <= 0:
                raise ValueError(f"下载的{kind}为空：{url}")
        check_download()
        part_path.replace(final_path)
        return final_path
    except httpx.HTTPError as exc:
        raise ValueError(f"下载网络{kind}失败：{url}（{exc}）") from exc
    finally:
        if part_path is not None:
            part_path.unlink(missing_ok=True)


def download_remote_video(
    url: str,
    output_dir: Path,
    *,
    timeout_s: float | None = None,
) -> Path:
    return _download_remote_media(
        url,
        output_dir,
        kind="视频",
        extensions=VIDEO_EXTENSIONS,
        content_suffixes=_VIDEO_CONTENT_SUFFIXES,
        fallback_suffix=".mp4",
        fallback_name="remote_video.mp4",
        timeout_s=timeout_s,
    )


def download_remote_image(
    url: str,
    output_dir: Path,
    *,
    timeout_s: float | None = None,
) -> Path:
    path = _download_remote_media(
        url,
        output_dir,
        kind="图片",
        extensions=IMAGE_EXTENSIONS,
        content_suffixes=_IMAGE_CONTENT_SUFFIXES,
        fallback_suffix=".png",
        fallback_name="remote_image.png",
        timeout_s=timeout_s,
    )
    detected = _image_suffix_from_header(path)
    if detected and path.suffix.lower() not in (
        {detected, ".jpeg"} if detected == ".jpg" else {detected}
    ):
        corrected = path.with_suffix(detected)
        path.replace(corrected)
        return corrected
    return path


def remove_temporary_downloads(paths: list[Path], runs_dir: Path = RUNS_DIR) -> None:
    root = (runs_dir / "downloads").resolve()
    for path in paths:
        try:
            candidate = path.resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue


def cleanup_item_downloads(item: dict, *, runs_dir: Path = RUNS_DIR) -> None:
    """Delete only downloads recorded by the evaluation runtime."""
    raw_paths = item.pop(TEMPORARY_DOWNLOAD_PATHS_FIELD, None) or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    paths = [Path(str(path)) for path in raw_paths if str(path).strip()]
    remove_temporary_downloads(paths, runs_dir)
