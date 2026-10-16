import asyncio
import hashlib
import io
import json
from collections import OrderedDict

import pytest
from fastapi import UploadFile

from auto_eval.web import history, source_files, tasks
from auto_eval.web.tasks import Task


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(tasks, "TASKS", OrderedDict())
    return history.HISTORY_DIR


def upload(content: bytes, filename: str = "原始数据.JSONL") -> dict:
    file = UploadFile(file=io.BytesIO(content), filename=filename)
    metadata = asyncio.run(source_files.store_upload(file))
    assert file.file.closed
    return metadata


@pytest.mark.parametrize("content,filename", [
    (b'\xef\xbb\xbf{"query":"hello"}\r\n\r\n', "原始数据.JSONL"),
    ('query,备注\r\n"hello","逗号,换行\r\n保留"\r\n'.encode("utf-16"), "数据.CSV"),
    (b"\x00\xff\xfe unchanged bytes \r\n", "数据.backup.json"),
])
def test_upload_and_repeated_runs_preserve_original_bytes(archive, content, filename):
    metadata = upload(content, filename)
    assert metadata == {"id": metadata["id"], "filename": filename, "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()}
    assert len(metadata["id"]) == 32
    assert (archive / "source_uploads" / f"{metadata['id']}.bin").read_bytes() == content
    for task_id in ("first-run", "second-run"):
        assert source_files.prepare_task_sources(task_id, source_file_id=metadata["id"]) == [metadata]
        assert source_files.source_file_path(task_id, metadata["id"]).read_bytes() == content
    assert source_files.source_file_path("first-run", metadata["id"]) != source_files.source_file_path("second-run", metadata["id"])


def test_metadata_survives_new_task_snapshot_reload_payload_and_old_sidecar(archive):
    metadata = upload(b"original\r\n")
    sources = source_files.prepare_task_sources("roundtrip", source_file_id=metadata["id"])
    task = tasks.new_task("rich_content", [{"query": "q"}], {}, task_id="roundtrip", source_files=sources)
    snapshot = history.load_snapshot(task.id)
    assert snapshot["source_files"] == [metadata]
    restored = tasks._task_from_snapshot(snapshot, task.id)
    assert restored.source_files == [metadata]
    assert history.snapshot_payload(history.task_to_snapshot(restored))["source_files"] == [metadata]
    path = history._find_task_path(task.id)
    meta_path = history._meta_path(path)
    meta_path.write_text(json.dumps({"meta_version": 1, "task_id": task.id}), encoding="utf-8")
    assert history.list_snapshots()[0]["source_files"] == [metadata]
    assert json.loads(meta_path.read_text(encoding="utf-8"))["meta_version"] == 2
    legacy = tasks._task_from_snapshot({"items": [], "mode": "compare"}, "legacy")
    assert legacy.source_files == []
    assert history.snapshot_payload({})["source_files"] == []


def test_reuse_has_independent_bytes_after_restart_and_source_history_deletion(archive):
    content = b'\xef\xbb\xbf{"original": true}\r\n'
    metadata = upload(content)
    originals = source_files.prepare_task_sources("original", source_file_id=metadata["id"])
    task = Task(id="original", mode="compare", items=[], options={}, source_files=originals)
    assert history.save_task(task)
    restored = tasks._task_from_snapshot(history.load_snapshot(task.id), task.id)
    reused = source_files.prepare_task_sources("reused", source_task=restored)
    assert reused == [metadata]
    reused_task = Task(id="reused", mode="compare", items=[], options={}, source_files=reused)
    assert history.save_task(reused_task)
    assert history.delete_snapshot(task.id)
    with pytest.raises(ValueError, match="不存在"):
        source_files.source_file_path(task.id, metadata["id"])
    assert source_files.source_file_path(reused_task.id, metadata["id"]).read_bytes() == content
    assert history.load_snapshot(reused_task.id)["source_files"] == [metadata]
    assert history.delete_snapshot(reused_task.id)
    assert not source_files._task_directory(reused_task.id).exists()
    # A user may rerun from the same browser even after both histories are deleted.
    assert source_files.prepare_task_sources("third-run", source_file_id=metadata["id"]) == [metadata]


def test_same_task_reuse_is_noop_and_missing_legacy_source_is_optional(archive, monkeypatch):
    metadata = upload(b"source")
    sources = source_files.prepare_task_sources("same", source_file_id=metadata["id"])
    task = Task(id="same", mode="rich_content", items=[], options={}, source_files=sources)
    def no_copy(*args):
        raise AssertionError("existing immutable archive should not be copied")
    monkeypatch.setattr(source_files.shutil, "copyfile", no_copy)
    assert source_files.prepare_task_sources(task.id, source_task=task) == sources
    assert source_files.prepare_task_sources("legacy", source_task=Task(id="legacy", mode="compare", items=[], options={})) == []
    assert source_files.prepare_task_sources("manual") == []


@pytest.mark.parametrize("file_id", ["", "../outside", "a" * 31, "A" * 32, "a" * 32 + "/../outside"])
def test_invalid_file_ids_are_rejected(archive, file_id):
    with pytest.raises(ValueError, match="ID 无效"):
        source_files.source_file_path("task", file_id)


def test_filename_controls_and_task_paths_cannot_escape_archive(archive):
    metadata = upload(b"bytes", "C:\\fakepath\\../中文 文件.JSONL\r\n\x00")
    assert metadata["filename"] == "中文 文件.JSONL"
    task_id = "../../outside/任务"
    source_files.prepare_task_sources(task_id, source_file_id=metadata["id"])
    path = source_files.source_file_path(task_id, metadata["id"])
    assert path.is_relative_to((archive / "source_files").resolve())
    assert metadata["filename"] not in str(path)
    keep = archive / "source_files" / "keep.txt"
    keep.write_bytes(b"unrelated")
    source_files.delete_task_sources(task_id)
    assert keep.read_bytes() == b"unrelated"


def test_changed_or_missing_archives_are_rejected(archive):
    metadata = upload(b"original")
    source_files.prepare_task_sources("task", source_file_id=metadata["id"])
    path = source_files.source_file_path("task", metadata["id"])
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="损坏"):
        source_files.source_file_path("task", metadata["id"])
    path.unlink()
    with pytest.raises(ValueError, match="不存在"):
        source_files.source_file_path("task", metadata["id"])
    with pytest.raises(ValueError, match="不存在"):
        source_files.prepare_task_sources("missing", source_file_id="a" * 32)


def test_upload_limit_removes_partial_archive_and_closes_upload(archive, monkeypatch):
    monkeypatch.setattr(source_files, "MAX_SOURCE_UPLOAD_BYTES", 3)
    file = UploadFile(file=io.BytesIO(b"four"), filename="large.json")
    with pytest.raises(ValueError, match="100MiB"):
        asyncio.run(source_files.store_upload(file))
    assert file.file.closed
    assert list((archive / "source_uploads").iterdir()) == []


def test_failed_batch_copy_rolls_back_only_new_files(archive, monkeypatch):
    entries = [upload(content) for content in (b"keep", b"rollback", b"fail")]
    for metadata in entries:
        source_files.prepare_task_sources("source", source_file_id=metadata["id"])
    source_task = Task(id="source", mode="compare", items=[], options={}, source_files=entries)
    source_files.prepare_task_sources("target", source_file_id=entries[0]["id"])
    original_write = source_files._write_metadata
    def fail_last(path, metadata):
        if metadata["id"] == entries[-1]["id"]:
            raise OSError("disk failure")
        original_write(path, metadata)
    monkeypatch.setattr(source_files, "_write_metadata", fail_last)
    with pytest.raises(OSError, match="disk failure"):
        source_files.prepare_task_sources("target", source_task=source_task)
    assert source_files.source_file_path("target", entries[0]["id"]).read_bytes() == b"keep"
    assert sorted(path.name for path in source_files._task_directory("target").iterdir()) == [
        f"{entries[0]['id']}.bin", f"{entries[0]['id']}.json"]
