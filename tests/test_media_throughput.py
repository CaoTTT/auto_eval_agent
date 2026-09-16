"""Remove repeated media work without changing selected visual evidence."""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from auto_eval import media, preparation
from auto_eval.config import VisualModeProfile
from auto_eval.web import video_prepare


@pytest.mark.parametrize("raw, expected", [(None, 4), ("1", 1), ("8", 8), ("16", 16)])
def test_media_worker_limit_is_independent_and_configurable(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AUTO_EVAL_MEDIA_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("AUTO_EVAL_MEDIA_CONCURRENCY", raw)
    assert preparation.preparation_concurrency() == expected


@pytest.mark.parametrize("raw", ["0", "17", "-1", "1.5", "auto", ""])
def test_invalid_media_worker_limit_is_reported(monkeypatch, raw):
    monkeypatch.setenv("AUTO_EVAL_MEDIA_CONCURRENCY", raw)
    with pytest.raises(ValueError, match="AUTO_EVAL_MEDIA_CONCURRENCY"):
        preparation.preparation_concurrency()


def test_feature_cache_reuses_exact_features_only_within_one_extraction(monkeypatch, tmp_path):
    path = tmp_path / "frame.jpg"
    pixels = np.random.default_rng(7).integers(0, 256, (400, 240, 3), dtype=np.uint8)
    Image.fromarray(pixels).save(path)
    functions = (media._signature, media._layout_signature, media._assistant_shell_signature)
    expected = [fn(path) for fn in functions]
    opened = []
    original_open = Image.open

    def record_open(*args, **kwargs):
        opened.append(args[0])
        return original_open(*args, **kwargs)

    monkeypatch.setattr(Image, "open", record_open)
    with media._cached_frame_features():
        for _ in range(3):
            for fn, baseline in zip(functions, expected):
                np.testing.assert_array_equal(fn(path), baseline)
    assert len(opened) == 3  # Previously nine JPEG decodes and six blurs.
    assert media._frame_features.get() is None

    # The same path may contain different evidence in a later extraction.
    Image.new("RGB", (240, 400), "white").save(path)
    with media._cached_frame_features():
        assert not np.array_equal(media._signature(path), expected[0])
    assert len(opened) == 4


def test_feature_cache_retained_memory_is_bounded_and_owns_views():
    cache = media._FrameFeatureCache(max_bytes=48)
    large = np.zeros((1024, 1024), dtype=np.float32)
    view = large[0, :8]
    first, second = ("pixels", Path("a.jpg")), ("pixels", Path("b.jpg"))
    cache.put(first, view)
    kept = cache.get(first)
    assert kept is not None and kept.flags.owndata and not kept.flags.writeable
    assert kept.base is None
    cache.put(second, view)
    assert cache.get(first) is None
    assert cache.get(second) is not None
    assert cache.size == 32


def test_cached_features_still_check_cooperative_cancellation(monkeypatch, tmp_path):
    path = tmp_path / "frame.jpg"
    Image.new("RGB", (20, 20), "white").save(path)
    with media._cached_frame_features():
        media._signature(path)

        def stopped():
            raise preparation.PreparationStopped("cancelled")

        monkeypatch.setattr(media, "check_preparation", stopped)
        with pytest.raises(preparation.PreparationStopped, match="cancelled"):
            media._signature(path)
    assert media._frame_features.get() is None


def _candidate_frames(video, work_dir, config, duration):
    """Several UI states, including a short popup and return to the shell."""
    candidates = []
    for index in range(24):
        image = Image.new("RGB", (240, 400), "white")
        draw = ImageDraw.Draw(image)
        if 4 <= index < 18:
            draw.rectangle((15, 65, 225, 365), fill=(30, 75, 135))
        if 8 <= index < 10:
            draw.rectangle((55, 140, 185, 245), fill=(240, 30, 30))
        path = work_dir / f"{index}.jpg"
        image.save(path, quality=90)
        source = "start-0.0s" if index == 0 else "terminal-0.3s" if index == 23 else "1fps"
        candidates.append(media._Candidate(float(index), path, source))
    return None, candidates


def test_feature_reuse_preserves_all_selected_frame_bytes_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "_extract_candidates", _candidate_frames)
    calls = []
    monkeypatch.setattr(media, "probe_duration", lambda path: calls.append(path) or 24.0)
    caching = media._cached_frame_features
    video = tmp_path / "video.mp4"
    config = media.KeyframeConfig(task_start_time=0, protected_sample_interval=0)

    monkeypatch.setattr(media, "_cached_frame_features", nullcontext)
    baseline = media.extract_scene_keyframes(video, tmp_path / "baseline", config=config)
    monkeypatch.setattr(media, "_cached_frame_features", caching)
    optimized = media.extract_scene_keyframes(video, tmp_path / "optimized", config=config, duration=24.0)

    assert len(calls) == 1, "known duration must not launch another probe"
    assert baseline and len(baseline) == len(optimized)
    assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in baseline] == [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in optimized
    ]
    assert json.loads((tmp_path / "baseline" / "keyframes.json").read_text()) == json.loads(
        (tmp_path / "optimized" / "keyframes.json").read_text()
    )


def test_compare_preparation_reuses_each_validated_video_duration(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "_extract_candidates", _candidate_frames)

    def unexpected_probe(path):
        pytest.fail("extractor launched a duplicate video probe")

    monkeypatch.setattr(media, "probe_duration", unexpected_probe)
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).touch()
    calls = []
    prepared = video_prepare.prepare_session_visual_compare_item(
        {"id": "case", "video1": "a.mp4", "video2": "b.mp4"},
        profile=VisualModeProfile(extraction={"algorithm_version": "test"}),
        session_name="test", item_index=0, total_items=1,
        base_dir=tmp_path, runs_dir=tmp_path / "runs",
        probe_fn=lambda path: calls.append(path.name) or 24.0,
    )
    assert calls == ["a.mp4", "b.mp4"]
    assert prepared["frames1"] and prepared["frames2"]
    assert prepared["duration1"] == prepared["duration2"] == 24.0


def test_custom_extractor_does_not_require_new_duration_argument(tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"video")
    def extract(video_path, frame_dir):
        frame = frame_dir / "kf_001.jpg"
        frame.write_bytes(b"test")
        return [frame]

    frames = video_prepare._extract_frames(
        tmp_path / "video.mp4", tmp_path / "frames", extract_fn=extract, duration=24.0,
    )
    assert len(frames) == 1
