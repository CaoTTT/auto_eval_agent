import hashlib
import asyncio
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from auto_eval import query_images
from auto_eval import remote_media
from auto_eval.preparation import run_preparation
from auto_eval.config import QueryImageConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.remote_media import (
    TEMPORARY_DOWNLOAD_PATHS_FIELD,
    cleanup_item_downloads,
    download_remote_image,
    download_remote_video,
    is_remote_url,
)
from auto_eval.web import video_prepare


def _image(path: Path, color=(20, 50, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 96), color).save(path)
    return path


def _profile() -> VisualModeProfile:
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))


def _download_from(path: Path, output_dir: Path, name: str = "download.png"):
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / name
    target.write_bytes(path.read_bytes())
    return target


def test_remote_query_image_is_snapshotted_and_temporary_download_is_removed(
    tmp_path, monkeypatch
):
    runs = tmp_path / "runs"
    monkeypatch.setattr(query_images, "RUNS_DIR", runs)
    source = _image(tmp_path / "source.png")
    url = "https://example.test/question.png"
    downloaded = []

    def download(_url, output_dir):
        target = _download_from(source, output_dir, f"download-{len(downloaded)}.png")
        downloaded.append(target)
        return target

    item = {"query": "q", "query_images": [url]}
    prepared = query_images.prepare_query_images(
        item,
        session_name="remote-query",
        cfg=QueryImageConfig(),
        download_fn=download,
    )
    meta = prepared["query_image_meta"][0]
    assert meta["source_url"] == url
    assert meta["original_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert Path(prepared["query_images"][0]).is_file()
    assert all(not path.exists() for path in downloaded)

    repeated = query_images.prepare_query_images(
        {**prepared, "query_images": [url]},
        session_name="remote-query",
        cfg=QueryImageConfig(),
        download_fn=download,
    )
    assert repeated["query_image_meta"][0]["image_id"] == meta["image_id"]
    assert all(not path.exists() for path in downloaded)


def test_remote_query_image_changed_content_fails_without_leaking_download(
    tmp_path, monkeypatch
):
    runs = tmp_path / "runs"
    monkeypatch.setattr(query_images, "RUNS_DIR", runs)
    source = _image(tmp_path / "source.png")
    url = "https://example.test/question.png"
    downloaded = []

    def download(_url, output_dir):
        target = _download_from(source, output_dir, f"download-{len(downloaded)}.png")
        downloaded.append(target)
        return target

    prepared = query_images.prepare_query_images(
        {"query": "q", "query_images": [url]},
        session_name="remote-query-change",
        cfg=QueryImageConfig(),
        download_fn=download,
    )
    _image(source, (90, 30, 10))
    with pytest.raises(query_images.QueryImageError, match="发生变化"):
        query_images.prepare_query_images(
            {**prepared, "query_images": [url]},
            session_name="remote-query-change",
            cfg=QueryImageConfig(),
            download_fn=download,
        )
    assert all(not path.exists() for path in downloaded)


def test_compare_remote_videos_keep_urls_and_clean_runtime_downloads(tmp_path):
    calls = []

    def download(url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"video-{len(calls)}.mp4"
        target.write_bytes(url.encode("utf-8"))
        calls.append((url, target))
        return target

    def extract(video, directory, **_kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        frame = directory / "kf_001.jpg"
        frame.write_bytes(video.read_bytes())
        return [frame]

    item = {
        "id": "remote-video",
        "query": "q",
        "video1": "https://example.test/one.mp4",
        "video2": "https://example.test/two.mp4",
    }
    prepared = video_prepare.prepare_session_visual_compare_item(
        item,
        profile=_profile(),
        session_name="remote-video",
        item_index=0,
        total_items=1,
        base_dir=tmp_path,
        runs_dir=tmp_path / "runs",
        probe_fn=lambda _path: 12.0,
        extract_fn=extract,
        download_fn=download,
    )
    assert prepared["video1"] == item["video1"]
    assert prepared["video2"] == item["video2"]
    assert prepared["video_source1"]["path"] == item["video1"]
    assert prepared["video_source2"]["input_path"] == item["video2"]
    assert prepared["media"] == [item["video1"], item["video2"]]
    temporary = [Path(path) for path in prepared[TEMPORARY_DOWNLOAD_PATHS_FIELD]]
    assert len(temporary) == 2 and all(path.is_file() for path in temporary)

    cleanup_item_downloads(prepared, runs_dir=tmp_path / "runs")
    assert TEMPORARY_DOWNLOAD_PATHS_FIELD not in prepared
    assert all(not path.exists() for _, path in calls)


def test_compare_remote_video_failure_cleans_partial_downloads(tmp_path):
    downloads = []

    def download(url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"video-{len(downloads)}.mp4"
        target.write_bytes(url.encode("utf-8"))
        downloads.append(target)
        return target

    with pytest.raises(ValueError, match="时长为0"):
        video_prepare.prepare_session_visual_compare_item(
            {
                "query": "q",
                "video1": "https://example.test/one.mp4",
                "video2": "https://example.test/two.mp4",
            },
            profile=_profile(),
            session_name="remote-video-failure",
            item_index=0,
            total_items=1,
            base_dir=tmp_path,
            runs_dir=tmp_path / "runs",
            probe_fn=lambda _path: 0.0,
            extract_fn=lambda *_args, **_kwargs: pytest.fail("must not extract"),
            download_fn=download,
        )
    assert len(downloads) == 1
    assert not downloads[0].exists()


def test_compare_remote_long_screenshot_uses_immutable_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATION_VIDEO_ROOTS", str(tmp_path))
    source = _image(tmp_path / "remote-shot.png")
    url = "https://example.test/shot.png"
    downloaded = []

    def download(_url, output_dir):
        target = _download_from(source, output_dir, f"image-{len(downloaded)}.png")
        downloaded.append(target)
        return target

    item = {
        "query": "q",
        "screenshot1": url,
        "screenshot2": url,
    }
    prepared = video_prepare.prepare_session_long_screenshot_item(
        item,
        profile=_profile(),
        session_name="remote-shot",
        item_index=0,
        total_items=1,
        base_dir=tmp_path,
        runs_dir=tmp_path / "runs",
        download_fn=download,
    )
    assert prepared["screenshot1"] == url
    assert prepared["screenshot2"] == url
    assert all(not path.exists() for path in downloaded)
    for number in (1, 2):
        meta = prepared[f"screenshot_meta{number}"]
        assert meta["source_url"] == url
        local = tmp_path / meta["original_path"]
        assert local.is_file()
        assert local.name.startswith("source_")


def test_remote_url_detection_is_limited_to_http_and_https():
    assert is_remote_url("https://example.test/a.png")
    assert is_remote_url("HTTP://example.test/a.png")
    assert not is_remote_url("data/image.png")
    assert not is_remote_url("file:///tmp/a.png")


def test_remote_media_downloads_http_files_to_temporary_paths(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image = _image(source_dir / "question.png")
    mismatched_image = source_dir / "question.jpg"
    mismatched_image.write_bytes(image.read_bytes())
    video = source_dir / "answer.mp4"
    video.write_bytes(b"video-bytes")

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietHandler, directory=str(source_dir)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        downloaded_image = download_remote_image(
            f"{base_url}/question.png",
            tmp_path / "downloads" / "images",
        )
        downloaded_video = download_remote_video(
            f"{base_url}/answer.mp4",
            tmp_path / "downloads" / "videos",
        )
        corrected_image = download_remote_image(
            f"{base_url}/question.jpg",
            tmp_path / "downloads" / "corrected",
        )
        assert downloaded_image.suffix == ".png"
        assert downloaded_image.read_bytes() == image.read_bytes()
        assert corrected_image.suffix == ".png"
        assert corrected_image.read_bytes() == image.read_bytes()
        assert downloaded_video.suffix == ".mp4"
        assert downloaded_video.read_bytes() == video.read_bytes()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _stream_chunks(monkeypatch, chunks):
    @contextmanager
    def stream(*_args, **_kwargs):
        yield SimpleNamespace(
            url="https://example.test/video.mp4",
            headers={},
            raise_for_status=lambda: None,
            iter_bytes=chunks,
        )

    monkeypatch.setattr(remote_media.httpx, "stream", stream)


@pytest.mark.parametrize("cancel", [False, True])
async def test_download_obeys_preparation_stop_and_removes_partial_files(
    tmp_path, monkeypatch, cancel
):
    started = threading.Event()
    consumed = []

    def chunks():
        started.set()
        for n in range(100):
            time.sleep(0.005)
            consumed.append(n)
            yield b"video"

    _stream_chunks(monkeypatch, chunks)
    output_dir = tmp_path / "downloads"
    worker = asyncio.create_task(run_preparation(
        download_remote_video, "https://example.test/video.mp4", output_dir,
        timeout=10 if cancel else 0.03,
    ))
    if cancel:
        await asyncio.to_thread(started.wait, 1)
        worker.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await worker
    assert len(consumed) < 100
    assert not list(output_dir.iterdir())


def test_download_enforces_total_duration_for_continuous_stream(tmp_path, monkeypatch):
    clock = [0.0]
    consumed = []

    def chunks():
        for n in range(10):
            clock[0] += 0.4
            consumed.append(n)
            yield b"video"

    monkeypatch.setattr(remote_media, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _stream_chunks(monkeypatch, chunks)
    output_dir = tmp_path / "downloads"
    with pytest.raises(TimeoutError, match="下载超时"):
        download_remote_video("https://example.test/video.mp4", output_dir, timeout_s=1)
    assert len(consumed) == 3
    assert not list(output_dir.iterdir())


def test_download_cleanup_cannot_remove_history_or_follow_external_symlinks(tmp_path):
    runs = tmp_path / "runs"
    downloads = runs / "downloads"
    downloads.mkdir(parents=True)
    history = runs / "other-task.json"
    history.write_text("history", encoding="utf-8")
    temporary = downloads / "video.mp4"
    temporary.write_bytes(b"download")
    link = downloads / "history.mp4"
    link.symlink_to(history)
    item = {TEMPORARY_DOWNLOAD_PATHS_FIELD: [str(history), str(link), str(temporary)]}

    cleanup_item_downloads(item, runs_dir=runs)

    assert history.read_text(encoding="utf-8") == "history"
    assert link.is_symlink()
    assert not temporary.exists()
