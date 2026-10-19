"""Large batches must start judging before the whole media queue is prepared."""
import asyncio

import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualExtractionConfig
from auto_eval.preparation import PreparationLimiter
from auto_eval.web import runner
from auto_eval.web.scheduler import ResizableLimiter
from auto_eval.web.tasks import Task


@pytest.mark.asyncio
async def test_large_batch_starts_judging_while_later_media_waits(monkeypatch):
    state = {"prepared": 0, "active": 0, "peak": 0}
    first_model_prepared = []

    monkeypatch.setattr(runner, "PREP_LIMITER", PreparationLimiter(4))
    monkeypatch.setattr(runner, "PIPELINE_LIMITER", ResizableLimiter(8))

    class Client:
        def __init__(self, cfg):
            self.cfg = cfg

        async def aclose(self):
            pass

    async def fake_prepare(fn, item, **kwargs):
        await runner.PREP_LIMITER.acquire()
        try:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.02)
            state["prepared"] += 1
            state["active"] -= 1
            return {**item, "frames": ["ready.png"]}
        finally:
            runner.PREP_LIMITER.release()

    async def evaluate(mode, index, item, **kwargs):
        if not first_model_prepared:
            first_model_prepared.append(state["prepared"])
        return {"item_id": item.get("id", f"q{index}"), "query": item.get("query", "")}

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "run_preparation", fake_prepare)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *_args, **_kwargs: None)

    items = [{"id": f"q{index}", "query": f"case{index}"} for index in range(32)]
    task = Task(id="media-precedence", mode="rich_content", items=items, options={})
    cfg = AppConfig(
        judges=[JudgeConfig(name="judge", model="test")],
        visual_modes={"rich_content": {"extraction": VisualExtractionConfig(algorithm_version="test")}},
    )

    await asyncio.wait_for(runner._run(task, cfg), timeout=10)

    assert len(task.results) == 32
    assert all(not result.get("error") for result in task.results), task.results
    assert state["prepared"] == 32
    assert state["peak"] <= 4
    assert first_model_prepared
    assert first_model_prepared[0] <= 4
    assert first_model_prepared[0] < 32
