"""Archive uploaded dataset files without changing their original bytes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import UploadFile


MAX_SOURCE_UPLOAD_BYTES = 100 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024
_FILE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _history_directory() -> Path:
    # Resolve this at call time so tests and configured history locations agree.
    from . import history

    return history.HISTORY_DIR


def _file_id(value: str) -> str:
    if not isinstance(value, str) or not _FILE_ID.fullmatch(value):
        raise ValueError("源文件 ID 无效")
    return value


def _filename(value: str | None) -> str:
    name = re.split(r"[\\/]", value or "")[-1]
    name = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name)
    return name if name not in {"", ".", ".."} else "source_file"


def _within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("源文件归档路径无效")
    return resolved


def _task_directory(task_id: str) -> Path:
    if not isinstance(task_id, str) or not task_id or len(task_id) > 128:
        raise ValueError("任务 ID 无效")
    root = _history_directory() / "source_files"
    name = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:32]
    return _within(root / name, root)


def _paths(directory: Path, file_id: str) -> tuple[Path, Path]:
    file_id = _file_id(file_id)
    return (_within(directory / f"{file_id}.bin", directory),
            _within(directory / f"{file_id}.json", directory))


def _write_metadata(path: Path, metadata: dict) -> None:
    temporary = path.with_name(f".{uuid.uuid4().hex[:12]}.tmp")
    try:
        temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_content(path: Path, metadata: dict) -> None:
    try:
        if path.stat().st_size != metadata["size"]:
            raise ValueError("归档源文件已损坏")
        digest = hashlib.sha256()
        with path.open("rb") as content:
            for chunk in iter(lambda: content.read(_UPLOAD_CHUNK), b""):
                digest.update(chunk)
        if digest.hexdigest() != metadata["sha256"]:
            raise ValueError("归档源文件已损坏")
    except FileNotFoundError as exc:
        raise ValueError("源文件不存在或已删除") from exc


def _read_archive(directory: Path, file_id: str) -> tuple[Path, dict]:
    content_path, metadata_path = _paths(directory, file_id)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("源文件不存在或已删除") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("源文件归档信息无效") from exc
    if (not isinstance(metadata, dict) or metadata.get("id") != file_id
            or not isinstance(metadata.get("filename"), str)
            or type(metadata.get("size")) is not int or metadata["size"] < 0
            or not isinstance(metadata.get("sha256"), str)
            or not _SHA256.fullmatch(metadata["sha256"])):
        raise ValueError("源文件归档信息无效")
    metadata = {key: metadata[key] for key in ("id", "filename", "size", "sha256")}
    metadata["filename"] = _filename(metadata["filename"])
    _verify_content(content_path, metadata)
    return content_path, metadata


async def store_upload(file: UploadFile) -> dict:
    """Stream an upload into immutable staging, retaining its name and hash."""
    directory = _history_directory() / "source_uploads"
    directory.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    content_path, metadata_path = _paths(directory, file_id)
    temporary = directory / f".{uuid.uuid4().hex[:12]}.tmp"
    size = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("wb") as content:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SOURCE_UPLOAD_BYTES:
                    raise ValueError("源文件过大，限制 ≤100MiB")
                digest.update(chunk)
                content.write(chunk)
        metadata = {"id": file_id, "filename": _filename(file.filename),
                    "size": size, "sha256": digest.hexdigest()}
        os.replace(temporary, content_path)
        _write_metadata(metadata_path, metadata)
        return metadata
    except BaseException:
        temporary.unlink(missing_ok=True)
        content_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def prepare_task_sources(task_id: str, *, source_file_id: str = "", source_task=None) -> list[dict]:
    """Give a task independent copies of staged or previously archived sources."""
    if source_file_id:
        inputs = [_read_archive(_history_directory() / "source_uploads", source_file_id)]
    elif source_task is not None:
        inputs = []
        seen = set()
        for entry in getattr(source_task, "source_files", []) or []:
            file_id = _file_id(entry.get("id") if isinstance(entry, dict) else None)
            if file_id not in seen:
                inputs.append(_read_archive(_task_directory(source_task.id), file_id))
                seen.add(file_id)
    else:
        return []
    if not inputs:
        return []
    directory = _task_directory(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    temporary: Path | None = None
    try:
        for original, metadata in inputs:
            target, metadata_path = _paths(directory, metadata["id"])
            if target.exists() or metadata_path.exists():
                _, existing = _read_archive(directory, metadata["id"])
                if existing != metadata:
                    raise ValueError("任务已有不同的同 ID 源文件")
                continue
            temporary = directory / f".{uuid.uuid4().hex[:12]}.tmp"
            shutil.copyfile(original, temporary)
            _verify_content(temporary, metadata)
            os.replace(temporary, target)
            temporary = None
            created.append(target)
            _write_metadata(metadata_path, metadata)
            created.append(metadata_path)
        return [dict(metadata) for _, metadata in inputs]
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def source_file_path(task_id: str, file_id: str) -> Path:
    """Return a validated task-owned file, rejecting changed or missing bytes."""
    return _read_archive(_task_directory(task_id), file_id)[0]


def delete_task_sources(task_id: str) -> None:
    """Delete only the resolved directory owned by this exact task ID."""
    directory = _task_directory(task_id)
    root = (_history_directory() / "source_files").resolve()
    if directory.parent != root or not _FILE_ID.fullmatch(directory.name):
        raise ValueError("源文件归档路径无效")
    if directory.exists():
        shutil.rmtree(directory)
