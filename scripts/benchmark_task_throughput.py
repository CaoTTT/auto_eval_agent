"""Compare real old/new limiters on fixed synthetic 152-case workloads, offline.

Run with the project Python and PYTHONPATH=src. Neither real models nor network
services are used. Prompt text and tokenizer counts are real; usage, first-byte
delay and completion latency below are controlled simulation assumptions.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import heapq
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import types

from PIL import Image

from auto_eval import request_throttle as current
from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.token_estimation import count_text_tokens


ROOT = Path(__file__).resolve().parents[1]


def baseline_module(ref):
    source = subprocess.check_output([
        "git", "-c", f"safe.directory={ROOT.as_posix()}", "show",
        f"{ref}:src/auto_eval/request_throttle.py",
    ], cwd=ROOT).decode("utf-8")
    module = types.ModuleType("auto_eval._benchmark_baseline")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{ref}/request_throttle.py", "exec"), module.__dict__)
    return module


class Simulation:
    def __init__(self):
        self.now = 0.0
        self.events = []
        self.sequence = 0

    def __call__(self):
        return self.now

    def schedule(self, delay, callback):
        self.sequence += 1
        heapq.heappush(self.events, (self.now + delay, self.sequence, callback))

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.schedule(delay, lambda: future.set_result(None) if not future.done() else None)
        await future

    async def drive(self, job):
        for _ in range(100_000):
            # Flush event/lock/timer continuations before advancing virtual time.
            for _ in range(24):
                await asyncio.sleep(0)
            if job.done() and not self.events:
                return job.result()
            if not self.events:
                raise RuntimeError("limiter made no progress with no scheduled completion")
            self.now = self.events[0][0]
            while self.events and self.events[0][0] <= self.now:
                _, _, callback = heapq.heappop(self.events)
                callback()
        raise RuntimeError("simulation did not terminate")


def workload(profile, frames, output, response_s):
    protocol = resolve_compare_protocol(profile)
    system = protocol.system_template.render(persona="终端用户", product_count=2,
                                              evidence_mode="video_frames")
    user = protocol.user_template.render(
        evaluation_datetime="2026-09-15T12:00:00+08:00", question="请对比回答质量。",
        context="", product_count=2, context1="", context2="", context3="",
        answer1="产品一回答", answer2="产品二回答", answer3="",
        frame_count1=frames // 2, frame_count2=frames // 2, frame_count3=0,
        evidence_mode="video_frames", image_count1=frames // 2, image_count2=frames // 2,
    )
    picture = io.BytesIO()
    Image.new("RGB", (1280, 720), "white").save(picture, "JPEG")
    url = "data:image/jpeg;base64," + base64.b64encode(picture.getvalue()).decode()
    content = [{"type": "text", "text": user}] + [
        {"type": "image_url", "image_url": {"url": url}} for _ in range(frames)
    ]
    kwargs = {"model": current.MODEL, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": content},
    ]}
    # Explicit synthetic usage assumption: official text vocabulary plus a
    # fixed 32-pixel image grid; it is not a measured provider billing response.
    assumed_input = 272 + count_text_tokens(system) + count_text_tokens(user) + frames * (40 * 23 + 2)
    return kwargs, assumed_input, output, response_s


async def simulate(module, args, count):
    kwargs, prompt_tokens, output_tokens, latency = args
    clock = Simulation()
    throttle = module.RequestThrottle(clock=clock, sleep=clock.sleep)
    proxy = module.estimate_input_tokens(kwargs)
    sends, completions = [], []
    active = peak = 0

    async def issue():
        nonlocal active, peak
        for _ in range(count):
            options = {"input_proxy": proxy, "kind": "vision"}
            if hasattr(throttle, "confirm_input"):
                options.update(input_tokens=throttle.estimate_input(proxy, "vision"), reestimate=True)
            record = await throttle.acquire(throttle.estimate(proxy, "vision"), **options)
            throttle.headers_sent(record)
            sends.append(clock.now)
            active += 1
            peak = max(peak, active)
            if hasattr(throttle, "confirm_input"):
                clock.schedule(5, lambda record=record: throttle.confirm_input(record))

            def finish(record=record):
                nonlocal active
                throttle.finish(record, {"prompt_tokens": prompt_tokens,
                                         "completion_tokens": output_tokens})
                completions.append(clock.now)
                active -= 1

            clock.schedule(latency, finish)

    await clock.drive(asyncio.create_task(issue()))
    peak_second = max(sum(t <= other <= t + 1 for other in sends) for t in sends)
    assert len(completions) == count and peak_second <= 9
    return {"seconds": round(max(completions), 2), "first_result_s": round(min(completions), 2),
            "cases_per_minute": round(count * 60 / max(completions), 2),
            "peak_inflight": peak, "peak_requests_per_second": peak_second,
            "initial_input_estimate": proxy}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-ref", default="eab257e")
    parser.add_argument("--cases", type=int, default=152)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    baseline = baseline_module(options.baseline_ref)
    result = {"baseline_ref": options.baseline_ref, "cases": options.cases,
              "kind": "offline_virtual_clock_not_real_model", "workloads": []}
    start = time.perf_counter()
    for frames, output, latency in [(12, 3000, 90), (40, 6000, 120), (12, 12000, 150)]:
        args = workload("qa_competitor_compare@0.2-simplified-calibrated", frames, output, latency)
        old = await simulate(baseline, args, options.cases)
        new = await simulate(current, args, options.cases)
        row = {"frames": frames, "assumed_prompt_tokens": args[1],
               "assumed_output_tokens": output, "assumed_response_headers_s": 5,
               "assumed_response_s": latency,
               "old": old, "new": new, "speedup": round(old["seconds"] / new["seconds"], 3)}
        result["workloads"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    result["benchmark_wall_seconds"] = round(time.perf_counter() - start, 3)
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
