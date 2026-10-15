"""Large batches must start judging before the whole media queue is prepared."""
import asyncio
import json
import threading
import time

import pytest
from PIL import Image

from auto_eval import request_throttle
from auto_eval.config import AppConfig, JudgeConfig, VisualModeProfile
from auto_eval.judges import visual_compare_judge
from auto_eval.judges.compare_protocols import (
    DEFAULT_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V03_COMPARE_PROTOCOL_ID,
)
from auto_eval.web import runner
from auto_eval.web.tasks import Task
from test_request_transport import LoopbackProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_id", [
    DEFAULT_COMPARE_PROTOCOL_ID,
    V02_CALIBRATED_COMPARE_PROTOCOL_ID,
    V03_COMPARE_PROTOCOL_ID,
])
@pytest.mark.parametrize("evidence_mode", ["video_frames", "long_screenshot"])
async def test_large_compare_batch_starts_judging_while_later_media_waits(monkeypatch, protocol_id, evidence_mode):
    """Exercise both runner media preparation and the real judge encoding phase.

    All 152 cases start together, with the production 128-case/4-media limits.
    A later case's media preparation must not get ahead of every ready case's
    final image encoding; doing so delays the first result by ~32 media batches.
    """
    state = {"prepared": 0, "active": 0, "peak": 0}
    guard = threading.Lock()
    first_model = []
    calls = []

    def media_work(delay):
        with guard:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            time.sleep(delay)
        finally:
            with guard:
                state["active"] -= 1

    def prepare(item, **kwargs):
        media_work(.02)
        with guard:
            state["prepared"] += 1
        prepared = {**item, "frames1": ["a.png"], "frames2": ["b.png"], "frame_count": 2}
        if evidence_mode == "long_screenshot":
            for number, path in ((1, "a.png"), (2, "b.png")):
                prepared[f"screenshot_meta{number}"] = {
                    "split_status": "original", "split_count": 1,
                    "original_width": 40, "original_height": 40,
                    "boundaries": [], "estimated_image_tokens": 4,
                    "slices": [{"path": path, "start_y": 0, "end_y": 40,
                                "width": 40, "height": 40}],
                }
        return prepared

    def encode(*args, **kwargs):
        media_work(.001)
        return "data:image/png;base64,c3ludGhldGlj"

    class Client:
        def __init__(self, cfg):
            self.cfg, self.model, self.persona = cfg, cfg.model, "test"

        async def complete(self, system, user, **kwargs):
            if not first_model:
                with guard:
                    first_model.append(state["prepared"])
            calls.append(system)
            return json.dumps({"product_count": 2})

        async def aclose(self):
            pass

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "prepare_session_visual_compare_item", prepare)
    monkeypatch.setattr(runner, "prepare_session_long_screenshot_item", prepare)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(visual_compare_judge, "encode_frame", encode)
    monkeypatch.setattr(visual_compare_judge, "encode_original_image", encode)
    task = Task(
        id="large-media-pipeline", mode="compare", evaluation_profile=protocol_id,
        items=[{"id": f"q{i}", "query": f"case{i}", "video1": "a.mp4", "video2": "b.mp4",
                "evidence_mode": evidence_mode}
               for i in range(152)],
        options={"concurrency": 128},
    )
    cfg = AppConfig(
        judges=[JudgeConfig(name="judge_2", model="qwen3.5-397b-a17b",
                            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")],
        visual_modes={"rich_content": VisualModeProfile(extraction={"algorithm_version": "test"})},
    )
    await asyncio.wait_for(runner._run(task, cfg), timeout=15)

    assert len(calls) == task.done_total == 152
    assert not any(result.get("error") for result in task.results)
    assert state["peak"] <= 4
    assert first_model[0] <= 8, f"first judge waited for {first_model[0]} media preparations"
    assert all(result["evaluation_profile"] == protocol_id for result in task.results)


@pytest.mark.asyncio
async def test_calibrated_judge_completes_through_real_paced_sdk_and_local_http(monkeypatch, tmp_path):
    """A real calibrated prompt traverses the SDK/header limiter and yields a result."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    throttle = request_throttle.RequestThrottle()
    monkeypatch.setattr(request_throttle, "shared_throttle", lambda cfg: throttle)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    frame = tmp_path / "evidence.png"
    Image.new("RGB", (40, 40), "white").save(frame)
    answer = {
        "product_count": 2,
        "answer1_input_status": "complete", "answer2_input_status": "complete",
        "answer1_response_gate": "pass", "answer2_response_gate": "pass",
        "answer1_safety_gate": "pass", "answer2_safety_gate": "pass",
        "understanding_applicable": True,
        "understanding_verification_status": "not_required",
        "answer1_understanding_score": 5, "answer2_understanding_score": 4,
    }

    async def respond(request, writer):
        def event(choices, usage=None):
            chunk = {"id": "calibrated-local", "object": "chat.completion.chunk",
                     "created": 0, "model": request_throttle.MODEL, "choices": choices}
            if usage is not None:
                chunk["usage"] = usage
            return b"data: " + json.dumps(chunk).encode() + b"\n\n"

        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                     b"Connection: close\r\n\r\n")
        writer.write(event([{"index": 0, "delta": {"content": json.dumps(answer)},
                             "finish_reason": None}]))
        writer.write(event([{"index": 0, "delta": {}, "finish_reason": "stop"}]))
        writer.write(event([], {"prompt_tokens": 20000, "completion_tokens": 100,
                                "total_tokens": 20100}))
        writer.write(b"data: [DONE]\n\n")
        await writer.drain()

    async with LoopbackProvider(respond) as provider:
        cfg = AppConfig(
            judges=[JudgeConfig(name="judge_2", model=request_throttle.MODEL,
                                base_url=provider.url, total_timeout_s=5, max_attempts=1)],
            visual_modes={"rich_content": VisualModeProfile(extraction={"algorithm_version": "test"})},
        )
        task = Task(
            id="calibrated-paced-sdk", mode="compare", options={"eval_timeout_s": 10},
            evaluation_profile=V02_CALIBRATED_COMPARE_PROTOCOL_ID,
            items=[{"id": "q1", "query": "calibrated test", "product_count": 2,
                    "frames1": [str(frame)], "frames2": [str(frame)]}],
        )
        await asyncio.wait_for(runner._run(task, cfg), timeout=15)

        assert task.done_total == len(task.results) == len(provider.requests) == 1
        result = task.results[0]
        assert not result.get("error"), result
        assert result["evaluation_profile"] == V02_CALIBRATED_COMPARE_PROTOCOL_ID
        assert result["bundle_revision"] == "0.2.3"
        assert result["understanding_rank_groups"] == [["product1"], ["product2"]]
        request = provider.requests[0]
        assert "qa_competitor_compare/0.2-simplified-calibrated" in request.body["messages"][0]["content"]
        assert request.headers["x-dashscope-wait-timeout"] == "30"
        assert request.body["stream"] is True
        snapshot = throttle.snapshot()
        assert snapshot["total_requests"] == snapshot["total_successes"] == 1
        assert snapshot["inflight"] == snapshot["pending"] == 0
        assert snapshot["reserved_tokens"] == 0
