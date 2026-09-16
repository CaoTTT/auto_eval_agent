"""The per-case judge deadline defaults to 900s and respects task overrides."""
import pytest

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.web import runner
from auto_eval.web.tasks import Task


@pytest.mark.asyncio
@pytest.mark.parametrize("options, expected", [
    ({}, 900),
    ({"eval_timeout_s": 420}, 420),
    ({"eval_timeout": 600}, 600),
    ({"eval_timeout_s": 780, "eval_timeout": 600}, 780),
])
async def test_case_judge_receives_default_or_explicit_deadline(monkeypatch, options, expected):
    class Client:
        def __init__(self, config):
            self.config = config

    timeouts = []

    async def wait_for_active(awaitable, timeout):
        timeouts.append(timeout)
        return await awaitable

    async def evaluate(*args, **kwargs):
        return {"query": "test", "summary": "complete"}

    async def finish(*args):
        pass

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "wait_for_active", wait_for_active)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *args, **kwargs: None)
    item = {"id": "case-one", "query": "test", "frames": ["ready.png"]}
    task = Task(id="timeout-default", mode="rich_content", items=[item], options=options)
    cfg = AppConfig(judges=[JudgeConfig(name="test", model="test")])
    one, _ = runner._make_item_evaluator(task, cfg, on_result=finish)
    result = await one(0, item)
    assert not result.get("error")
    assert timeouts == [expected]
    assert task.options == options
