"""Compare old/new media preparation on an identical synthetic recording.

Run with the project Python and PYTHONPATH=src. Requires local FFmpeg/FFprobe,
but never calls a model or network service. Generated evidence/results go in a
new runs directory by default. CPU load affects wall time; run separately from
the test suite and other benchmarks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import types
import uuid

from auto_eval import media


ROOT = Path(__file__).resolve().parents[1]


def baseline_module(ref: str):
    git = ["git", "-c", f"safe.directory={ROOT.as_posix()}"]
    commit = subprocess.check_output(
        [*git, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=ROOT, text=True, encoding="utf-8",
    ).strip()
    source = subprocess.check_output(
        [*git, "show", f"{commit}:src/auto_eval/media.py"],
        cwd=ROOT, text=True, encoding="utf-8",
    )
    module = types.ModuleType("auto_eval._media_benchmark_baseline")
    module.__package__ = "auto_eval"
    sys.modules[module.__name__] = module
    exec(compile(source, f"{commit}/media.py", "exec"), module.__dict__)
    return commit, module


def generate_recording(video: Path) -> None:
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=white:s=720x1280:r=10:d=60", "-vf",
        "drawbox=x=45:y=160:w=630:h=950:color=blue:t=fill:enable='gte(t,8)*lt(t,48)',"
        "drawbox=x=130:y=430:w=460:h=350:color=red:t=fill:enable='gte(t,20)*lt(t,23)',"
        "drawbox=x=65:y=220:w=500:h=100:color=white:t=fill:enable='gte(t,30)*lt(t,42)'",
        "-c:v", "mpeg4", "-q:v", "2", str(video),
    ], check=True)


def measure(name: str, module, video: Path, directory: Path):
    config = module.KeyframeConfig(
        task_start_time=7, max_frames=20, max_edge=720,
        auto_task_end_confidence_threshold=2.0, protected_sample_interval=0,
    )
    start = time.perf_counter()
    # The session wrapper validates the task window using its own probe. The
    # baseline extractor repeats that probe; the new one reuses this duration.
    duration = module.probe_duration(video)
    frames = module.extract_scene_keyframes(
        video, directory, config=config, **({"duration": duration} if name == "after" else {}),
    )
    elapsed = time.perf_counter() - start
    if not frames:
        raise RuntimeError(f"{name} extraction produced no frames")
    metadata = json.loads((directory / "keyframes.json").read_text(encoding="utf-8"))
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in frames]
    return elapsed, metadata, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="eab257e")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--work-dir", type=Path, help="New directory for generated video and frames")
    parser.add_argument("--output", type=Path, help="Result JSON; defaults to WORK_DIR/results.json")
    options = parser.parse_args()
    if options.rounds < 1:
        parser.error("--rounds must be positive")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        parser.error("local ffmpeg and ffprobe must be available on PATH")
    baseline_commit, baseline = baseline_module(options.baseline_ref)
    work_dir = (options.work_dir or ROOT / "runs" / f"media-benchmark-{uuid.uuid4().hex}").resolve()
    work_dir.mkdir(parents=True, exist_ok=False)
    video = work_dir / "synthetic-ui.mp4"
    generate_recording(video)

    samples = {"before": [], "after": []}
    fingerprint = None
    for round_no in range(options.rounds):
        order = (("before", baseline), ("after", media))
        # Alternate order to reduce systematic cold-cache / thermal bias.
        if round_no % 2:
            order = tuple(reversed(order))
        for name, module in order:
            elapsed, metadata, hashes = measure(name, module, video, work_dir / f"{name}-{round_no}")
            current = (metadata, hashes)
            if fingerprint is None:
                fingerprint = current
            elif current != fingerprint:
                raise AssertionError("selected frame SHA256 or keyframe metadata changed")
            samples[name].append(elapsed)
            print(json.dumps({"mode": name, "round": round_no, "seconds": elapsed,
                              "frames": len(hashes)}), flush=True)

    medians = {name: statistics.median(times) for name, times in samples.items()}
    result = {
        "workload": "60-second synthetic 720x1280 UI recording, extraction plus session duration probe",
        "baseline_ref": options.baseline_ref,
        "baseline_commit": baseline_commit,
        "samples_seconds": samples,
        "median_seconds": medians,
        "speedup": medians["before"] / medians["after"],
        "time_reduction_percent": (1 - medians["after"] / medians["before"]) * 100,
        "all_frame_sha256_and_metadata_identical": True,
        "work_dir": str(work_dir),
        "note": "Offline media CPU/FFmpeg only, not end-to-end server or model throughput.",
    }
    output = options.output or work_dir / "results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
