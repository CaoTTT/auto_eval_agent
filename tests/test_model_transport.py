"""Task-selected judge settings reach every request without leaking reasoning."""
import json
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.config import AppConfig, JudgeConfig, RateLimitConfig
from auto_eval.judges.base import JudgeClient
from auto_eval.llm_stream import ProviderStreamError, stream_chat_completion
from auto_eval.request_throttle import (
    RequestThrottle, pacing_config, recommended_concurrency, shared_throttle,
)
from auto_eval.token_estimation import estimate_input_tokens


def judge(model="qwen3.5-397b-a17b", **kwargs):
    return JudgeConfig(
        name="judge_2", model=model,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["qwen3.5-397b-a17b", "qwen3.8-flash"])
@pytest.mark.parametrize("thinking", [True, False])
async def test_model_thinking_and_image_extras_reach_judging_and_repair(tmp_path, model, thinking):
    client = object.__new__(JudgeClient)
    client.cfg = judge(model, enable_thinking=thinking, vl_high_resolution_images=True)
    client.model = model
    client.trace_path = str(tmp_path / "calls.jsonl")
    requests = []

    async def create(kwargs, **_options):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"score": 4}'), finish_reason="stop",
        )], usage=None)

    client._llm_create_stream = create
    await client.complete("system", "user", user_images=["data:image/png;base64,AA=="])
    await client.repair_json('{"score": 4')
    assert all(request["model"] == model for request in requests)
    assert requests[0]["extra_body"] == {
        "enable_thinking": thinking, "vl_high_resolution_images": True,
    }
    assert requests[1]["extra_body"] == {"enable_thinking": thinking}
    records = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text("utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["model"] == model and record["enable_thinking"] is thinking for record in records)
    assert records[1]["purpose"] == "json_repair"


@pytest.mark.asyncio
async def test_none_omits_thinking_and_explicit_false_does_not_mutate_request():
    client = object.__new__(JudgeClient)
    client.cfg = judge()
    client.model = client.cfg.model
    client.trace_path = None
    requests = []

    async def create(kwargs, **_options):
        requests.append(kwargs)

    client._llm_create_stream = create
    request = {"extra_body": {"provider_option": 1}}
    await client._llm_create(request)
    client.cfg.enable_thinking = False
    await client._llm_create(request)
    assert requests[0]["extra_body"] == {"provider_option": 1}
    assert requests[1]["extra_body"] == {"provider_option": 1, "enable_thinking": False}
    assert request == {"extra_body": {"provider_option": 1}}


@pytest.mark.asyncio
async def test_failed_call_trace_retains_actual_thinking_choice(tmp_path):
    client = object.__new__(JudgeClient)
    client.cfg = judge("qwen3.8-flash", enable_thinking=False)
    client.model = client.cfg.model
    client.trace_path = str(tmp_path / "calls.jsonl")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    client._llm_create_stream = fail
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await client._llm_create({"model": client.model, "messages": []})
    record = json.loads((tmp_path / "calls.jsonl").read_text("utf-8"))
    assert record["model"] == "qwen3.8-flash"
    assert record["enable_thinking"] is False
    assert record["status"] == "error"


def chunk(content=None, reasoning=None, finish=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None),
        finish_reason=finish,
    )], usage=None, model="qwen3.8-flash")


class Completions:
    def __init__(self, attempts):
        self.attempts = attempts
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        events = self.attempts.pop(0)

        async def stream():
            for event in events:
                if isinstance(event, Exception):
                    raise event
                yield event
        return stream()


@pytest.mark.asyncio
async def test_reasoning_updates_state_without_leaking_or_replaying_failed_answer(monkeypatch):
    events = []
    monkeypatch.setattr("auto_eval.llm_stream.log_event", lambda *args, **kwargs: events.append((args, kwargs)))
    completions = Completions([
        [chunk(reasoning="private first thought"), chunk("discard"), httpx.RemoteProtocolError("closed")],
        [chunk(reasoning="private second thought"), chunk('{"score":'), chunk("4}", finish="stop")],
    ])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    output = []
    response = await stream_chat_completion(
        client, {"model": "qwen3.8-flash", "messages": [], "extra_body": {"enable_thinking": True}},
        callback=output.append, max_attempts=2, retry_base_s=0,
    )
    assert response.choices[0].message.content == '{"score":4}'
    assert output == ['{"score":', '4}']
    assert response.stream_stats["思考chunk数"] == 1
    assert response.stream_stats["首响应耗时"] <= response.stream_stats["首答案耗时"]
    assert any(args[1] == "模型思考中" for args, _kwargs in events)
    assert "private" not in json.dumps(events, ensure_ascii=False, default=str)
    assert "private" not in repr(response)


@pytest.mark.asyncio
async def test_truncated_reasoning_only_is_actionable_and_not_blindly_retried():
    completions = Completions([[chunk(reasoning="private", finish="length")]])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    with pytest.raises(ProviderStreamError, match="仅返回思考内容") as caught:
        await stream_chat_completion(client, {"model": "qwen3.8-flash", "messages": []}, max_attempts=4)
    assert not caught.value.retriable
    assert "private" not in str(caught.value.body)
    assert len(completions.requests) == 1


@pytest.mark.asyncio
async def test_reasoning_only_failure_still_settles_billed_token_usage():
    usage = {"prompt_tokens": 20, "completion_tokens": 20_000,
             "completion_tokens_details": {"reasoning_tokens": 20_000}}
    billed = chunk(reasoning="private", finish="length")
    billed.usage = usage
    completions = Completions([[billed]])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    throttle = RequestThrottle(warmup_s=0)
    with pytest.raises(ProviderStreamError, match="仅返回思考内容"):
        await stream_chat_completion(
            client, {"model": "qwen3.8-flash", "messages": []},
            throttle=throttle, max_attempts=1,
        )
    assert throttle.snapshot()["token_usage"] == 20_020
    assert throttle.snapshot()["reserved_tokens"] == 0
    assert throttle.snapshot()["inflight"] == 0


@pytest.mark.asyncio
async def test_model_buckets_are_separate_but_thinking_keys_and_repairs_share():
    old = shared_throttle(judge(enable_thinking=True, api_key_env="ONE"))
    assert old is shared_throttle(judge(enable_thinking=False, api_key_env="TWO"))
    flash = shared_throttle(judge("qwen3.8-flash", enable_thinking=True))
    assert flash is not old
    assert flash is shared_throttle(judge("qwen3.8-flash", enable_thinking=False))
    assert flash.snapshot()["model"] == "qwen3.8-flash"


@pytest.mark.asyncio
async def test_explicit_quota_groups_share_and_restrict_conflicting_budgets():
    rate = RateLimitConfig(group="one-account", rpm=1200, tpm=2_000_000, rps=20, max_inflight=80)
    throttle = shared_throttle(judge(rate_limit=rate))
    assert throttle.snapshot()["request_budget"] == 1200
    assert throttle.snapshot()["hard_second_limit"] == 20
    lower = rate.model_copy(update={"rpm": 120, "tpm": 100_000, "rps": 2, "max_inflight": 4})
    assert throttle is shared_throttle(judge("qwen3.8-flash", rate_limit=lower))
    snapshot = throttle.snapshot()
    assert snapshot["request_budget"] == 120
    assert snapshot["token_budget"] == 100_000
    assert snapshot["hard_second_limit"] == 2
    assert snapshot["max_inflight"] == 4
    assert snapshot["models"] == ["qwen3.5-397b-a17b", "qwen3.8-flash"]
    # Re-reading the larger profile cannot create a fresh/bigger budget.
    assert shared_throttle(judge(rate_limit=rate)).snapshot()["request_budget"] == 120


def test_flash_without_profile_does_not_inherit_legacy_env_budgets(monkeypatch):
    monkeypatch.setenv("AUTO_EVAL_BAILIAN_RPM", "400")
    monkeypatch.setenv("AUTO_EVAL_BAILIAN_TPM", "800000")
    cfg = judge("qwen3.8-flash")
    assert pacing_config(cfg)["rpm"] == 30_000
    assert pacing_config(cfg)["tpm"] == 20_000_000
    assert pacing_config(cfg)["rps"] == 500
    assert recommended_concurrency([cfg]) == 128
    assert pacing_config(judge())["rpm"] == 400
    assert pacing_config(judge())["tpm"] == 800_000
    configured = cfg.model_copy(update={"rate_limit": RateLimitConfig(rpm=1200, rps=20, max_inflight=32)})
    assert pacing_config(configured)["rpm"] == 1200
    assert recommended_concurrency([configured]) == 32


@pytest.mark.asyncio
async def test_configured_second_window_is_enforced():
    class Clock:
        now = 0.0
        def __call__(self):
            return self.now
        async def sleep(self, delay):
            self.now += delay
    clock = Clock()
    throttle = RequestThrottle(rpm=60_000, tpm=10**9, rps=3, warmup_s=0, clock=clock, sleep=clock.sleep)
    sends = []
    for _ in range(4):
        throttle._next_send = 0  # Isolate the hard rolling-second guard.
        reservation = await throttle.acquire(1)
        sends.append(reservation.sent)
        throttle.finish(reservation, {"total_tokens": 0})
    assert sends[:3] == [0, 0, 0]
    assert sends[3] > 1
    assert throttle.snapshot()["peak_requests_last_second"] == 3


def test_flash_token_estimate_does_not_assume_bundled_35_vocabulary(monkeypatch):
    monkeypatch.setattr("auto_eval.token_estimation.count_text_tokens", lambda _text: 1)
    messages = [{"content": "你好"}]
    assert estimate_input_tokens({"model": "qwen3.8-flash", "messages": messages}) == 271
    assert estimate_input_tokens({"model": "qwen3.5-397b-a17b", "messages": messages}) == 266


@pytest.mark.asyncio
async def test_long_thinking_output_reserves_full_budget_without_poisoning_future_requests():
    class Clock:
        now = 0.0
        def __call__(self):
            return self.now
        async def sleep(self, delay):
            self.now += delay

    clock = Clock()
    throttle = RequestThrottle(rpm=60, tpm=100_000, rps=1, max_tpm=100_000,
                               warmup_s=0, clock=clock, sleep=clock.sleep)
    first = await throttle.acquire(9000, input_proxy=100)
    throttle.finish(first, {"prompt_tokens": 100, "completion_tokens": 90_000})
    assert throttle._output_estimates["text"] == 112_500
    assert throttle.estimate(1) == 100_000
    next_request = await throttle.acquire(throttle.estimate(1))
    assert next_request.sent > 60
    assert next_request.tokens == 100_000
    assert throttle.snapshot()["inflight"] == 1
    throttle.finish(next_request, {"prompt_tokens": 1, "completion_tokens": 100})
    # A large payload must still fail, even after its output was capped.
    with pytest.raises(ValueError, match="超过本地每分钟预算"):
        await throttle.acquire(throttle.estimate(200_000))


def test_output_cap_uses_configured_ceiling_not_temporary_adaptive_target():
    throttle = RequestThrottle(tpm=800_000, max_tpm=900_000, adaptive=True)
    throttle._target_tpm = 400_000  # Temporary congestion feedback.
    throttle._output_estimates["text"] = 1_000_000
    assert throttle.estimate(1000) == 900_000
    throttle._validate_tokens(throttle.estimate(1000))
    with pytest.raises(ValueError, match="超过本地每分钟预算"):
        throttle._validate_tokens(throttle.estimate(900_001))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["rich_content", "compare"])
@pytest.mark.parametrize("thinking", [True, False])
async def test_runner_uses_frozen_model_before_global_protocol_and_retry_options(monkeypatch, mode, thinking):
    from auto_eval.judge_profiles import resolve_new_runtime
    from auto_eval.web import runner
    from auto_eval.web.tasks import Task

    original = AppConfig(judges=[judge(api_key_env="JUDGE_KEY")])
    runtime, _ = resolve_new_runtime(original, {
        "judge_model_profile": "bailian_qwen38_flash", "enable_thinking": thinking,
    })
    # Simulate service config changes and an old protocol after persistence.
    current = AppConfig(judges=[judge(enable_thinking=not thinking, api_key_env="CHANGED_KEY")])
    item = {"query": "q", "frames": ["ready"], "frames1": ["ready"], "frames2": ["ready"]}
    task = Task(
        id="frozen-transport", mode=mode, items=[item], options={"judges": ["judge_2"]},
        judge_runtime=json.loads(json.dumps(runtime)),
        protocol_manifest={"input_schema_version": "1.1", "judges": [{
            "name": "judge_2", "model": "qwen3.5-397b-a17b", "enable_thinking": not thinking,
        }]},
    )
    captures = []

    class Client:
        def __init__(self, cfg):
            captures.append(cfg)

    async def evaluate(*_args, **_kwargs):
        return {"index": 0, "total": 4}

    monkeypatch.setattr(runner, "JudgeClient", Client)
    monkeypatch.setattr(runner, "_eval_one", evaluate)
    monkeypatch.setattr(runner, "_persist_task", lambda *_args, **_kwargs: None)
    one, _clients = runner._make_item_evaluator(task, current, options={
        **task.options, "enable_thinking": not thinking, "judge_model_profile": "bailian_qwen35_397b",
    })
    await one(0, item)
    assert len(captures) == 1
    assert captures[0].model == "qwen3.8-flash"
    assert captures[0].enable_thinking is thinking
    assert captures[0].api_key_env == "JUDGE_KEY"
    assert task.results[0]["judge_runtime"] == runtime
