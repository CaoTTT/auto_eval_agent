"""On-demand draft previews with bounded opaque handles and path authorization."""
from __future__ import annotations

import hashlib
import mimetypes
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from ..config import QueryImageConfig
from ..query_images import authorized_path
from .operation_media import operation_video_roots, resolve_operation_video_path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class DatasetMedia:
    def __init__(self, *, capacity: int = 512, ttl: float = 1800):
        self.capacity = capacity
        self.ttl = ttl
        self.entries: OrderedDict[str, dict] = OrderedDict()
        self.lock = threading.Lock()

    def resolve(self, raw: str, role: str, policy: QueryImageConfig, base_dir: Path) -> Path:
        if not raw.strip() or "://" in raw:
            raise ValueError("请填写后端可读取的本地文件路径")
        if role == "query":
            path = authorized_path(raw, policy)
        elif role == "video":
            path = resolve_operation_video_path(raw, base_dir=base_dir)
        elif role == "screenshot":
            path = (base_dir / raw).resolve()
            if not any(path.is_relative_to(root) for root in operation_video_roots(base_dir)):
                raise ValueError("回答长截图不在授权目录中；请检查 OPERATION_VIDEO_ROOTS")
        else:
            raise ValueError("未知的证据类型")
        if not path.is_file():
            raise ValueError("文件不存在或无法读取")
        if role != "video" and path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("图片仅支持 PNG、JPEG、WebP")
        return path

    def register(self, raw: str, role: str, policy: QueryImageConfig, base_dir: Path,
                 expected_sha256: str = "") -> dict:
        path = self.resolve(raw, role, policy, base_dir)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if role != "video":
            with Image.open(path) as img:
                if img.format not in {"PNG", "JPEG", "WEBP"} or getattr(img, "n_frames", 1) != 1:
                    raise ValueError("文件不是静态 PNG、JPEG、WebP 图片")
                mime = Image.MIME[img.format]
                img.verify()
        digest = file_hash(path)
        if expected_sha256 and expected_sha256 != digest:
            raise ValueError("文件已发生变化，与历史评测使用的原图不一致")
        token = uuid.uuid4().hex
        with self.lock:
            now = time.monotonic()
            self.entries = OrderedDict((k, v) for k, v in self.entries.items() if v["expires"] > now)
            while len(self.entries) >= self.capacity:
                self.entries.popitem(last=False)
            self.entries[token] = {"path": str(path), "role": role, "mime": mime,
                                   "sha256": digest, "expires": now + self.ttl}
        return {"preview_url": f"/api/dataset-media/{token}", "filename": path.name,
                "download_url": f"/api/dataset-media/{token}?download=true"}

    def file(self, token: str, policy: QueryImageConfig, base_dir: Path) -> tuple[Path, str]:
        with self.lock:
            entry = self.entries.get(token)
            if not entry or entry["expires"] <= time.monotonic():
                raise ValueError("预览已过期，请收起后重新展开")
        path = self.resolve(entry["path"], entry["role"], policy, base_dir)
        if file_hash(path) != entry["sha256"]:
            raise ValueError("原文件已变化，请重新加载预览")
        return path, entry["mime"]
