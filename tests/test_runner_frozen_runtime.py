"""Runner-level regression: a persisted judge runtime wins over live config edits."""
import json

import pytest

from auto_eval.config import AppConfig, JudgeConfig
from auto_eval.judge_profiles import resolve_new_runtime
from auto_eval.web import runner
from auto_eval.web.tasks import Task


def judge(*, api_key_env="JUDGE_KEY", **kwargs):
    return JudgeConfig(
        name="judge_2",
        model="qwen3.5-397b-a17b",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env=api_key_env,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [True, False])
async def test_runner_evaluator_restores_frozen_judge_runtime(monkeypatch, thinking):
    original = AppConfig(judges=[judge(enable_thinking=thinking)])
    runtime, _ = resolve_new_runtime(original, {
        "judge_model_profile": "qwen3.8-flash",
        "enable_thinking": thinking,
    })
    frozen = json.loads(json.dumps(runtime))
    # Simulate a later service config change after the task was persisted.
    current = AppConfig(judges=[judge(enable_thinking=not thinking, api_key_env="CHANGED_KEY")])
    item = {"id": "q0", "query": "frozen runtime", "frames": ["ready"]}
    task = Task(
        id="frozen-runtime",
        mode="rich_content",
        items=[item],
        options={},
        judge_runtime=frozen,
    )
    captures = []

    class Client:
        def __init__(self, cfg):
            captures.append(cfg)

    async def evaluate(*_args, **_kwargs):
        return {"item_id": "q0", "query": "frozen runtime"}

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *_args, **_kwargs: None)

    one, _clients = runner._make_item_evaluator(task, current)
    result = await one(0, item)

    assert not result.get("error"), result
    assert len(captures) == 1
    assert captures[0].model == "qwen3.8-flash"
    assert captures[0].enable_thinking is thinking
    assert captures[0].api_key_env == "JUDGE_KEY"
    assert result["judge_runtime"] == frozen
