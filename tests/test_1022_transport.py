"""1022 request options and finish-reason guards, without live model calls."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from auto_eval.config import JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.judges.base import (
    JudgeClient,
    JudgeOutputParseError,
    JudgeOutputTruncatedError,
    merge_trace_web_result,
)
from auto_eval.llm_stream import ProviderStreamError


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", [
    "bailian_qwen35_397b", "bailian_qwen38_flash",
    "qwen3.5-397b-a17b-xiaohang", "qwen3.8-flash-xiaohang",
    "qwen3.5-397b-a17b-zimo", "qwen3.8-flash-zimo",
])
@pytest.mark.parametrize("mode", ["off", "json_object", "json_schema"])
async def test_1022_options_preserve_all_competitive_model_profiles(profile_id, mode):
    from auto_eval.config import load_config
    from auto_eval.judge_profiles import resolve_new_runtime, restore_runtime
    from auto_eval.judges.compare_1022_contract import response_format_1022

    app = load_config(Path(__file__).resolve().parents[1] / "config")
    app.judges[0].compare_structured_output = mode
    runtime, resolved = resolve_new_runtime(app, {"judge_model_profile": profile_id})
    cfg = resolved[0]
    client, requests = _client()
    client.cfg, client.model = cfg, cfg.model
    response_format = response_format_1022(mode, 2)
    await client.complete("system", "user", user_images=["data:image/png;base64,YWJj"],
                          response_format=response_format, require_complete=True)
    request, options = requests[0]
    assert options["defer_empty_length_trace"] is True
    assert request["model"] == cfg.model
    for key in ("temperature", "top_p", "seed", "frequency_penalty", "max_tokens", "n"):
        assert request[key] == getattr(cfg, key)
    assert request["max_tokens"] == 65536
    assert request["extra_body"]["top_k"] == 1
    assert request["extra_body"]["vl_high_resolution_images"] is True
    if cfg.enable_thinking is None:
        assert "enable_thinking" not in request["extra_body"]
    else:
        assert request["extra_body"]["enable_thinking"] is cfg.enable_thinking
    assert request.get("response_format") == response_format
    assert ("response_format" in request) == (mode != "off")
    assert cfg.total_timeout_s == 1800 and cfg.read_timeout_s == 300
    app.judges[0].compare_structured_output = "off"
    restored = restore_runtime(app, runtime)[0]
    assert restored.compare_structured_output == mode
    assert restored.enable_thinking == cfg.enable_thinking
    assert restored.max_tokens == cfg.max_tokens


def _client(*, content='{"result": "ok"}', finish_reason="stop", trace_path=None, **config):
    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="judge_2", model="test-model", seed=42, **config)
    client.model = "test-model"
    client.trace_path = str(trace_path) if trace_path else None
    requests = []

    async def create(kwargs, **options):
        requests.append((kwargs, options))
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )],
            usage=None,
        )

    client._llm_create = create
    return client, requests


def test_structured_output_is_explicit_and_configured_modes_are_validated():
    assert JudgeConfig(name="judge").compare_structured_output == "off"
    for mode in ("off", "json_object", "json_schema"):
        cfg = JudgeConfig(name="judge", compare_structured_output=mode)
        assert cfg.model_dump()["compare_structured_output"] == mode
    with pytest.raises(ValidationError):
        JudgeConfig(name="judge", compare_structured_output="auto")


@pytest.mark.asyncio
@pytest.mark.parametrize("response_format", [
    None,
    {"type": "json_object"},
    {"type": "json_schema", "json_schema": {
        "name": "compare_1022",
        "strict": True,
        "schema": {
            "type": "object", "properties": {"result": {"type": "string"}},
            "required": ["result"], "additionalProperties": False,
        },
    }},
])
async def test_complete_passes_only_explicit_response_format(response_format):
    client, requests = _client()
    assert await client.complete("system", "user", response_format=response_format) == '{"result": "ok"}'
    kwargs, _ = requests[0]
    if response_format is None:
        assert "response_format" not in kwargs
    else:
        assert kwargs["response_format"] == response_format
    assert kwargs["model"] == "test-model"
    assert kwargs["temperature"] == 0
    assert kwargs["seed"] == 42
    assert "extra_body" not in kwargs


@pytest.mark.asyncio
async def test_client_does_not_apply_compare_setting_to_legacy_calls():
    client, requests = _client(compare_structured_output="json_schema")
    await client.complete("system", "legacy protocol")
    assert "response_format" not in requests[0][0]


@pytest.mark.asyncio
async def test_response_format_preserves_visual_and_other_extra_options(tmp_path):
    client, requests = _client(
        vl_high_resolution_images=True, trace_path=tmp_path / "trace.jsonl",
    )
    original_sampling = {
        "temperature": 0,
        "extra_body": {"enable_thinking": True, "max_pixels": 4096},
    }
    client._sampling_kwargs = lambda: original_sampling
    response_format = {"type": "json_object"}
    await client.complete(
        "system", "user",
        user_images=["data:image/png;base64,YWJj"],
        user_image_refs=["source.png"],
        response_format=response_format,
        require_complete=True,
    )
    kwargs = requests[0][0]
    assert kwargs["response_format"] == response_format
    assert kwargs["extra_body"] == {
        "enable_thinking": True, "vl_high_resolution_images": True,
    }
    assert original_sampling["extra_body"]["max_pixels"] == 4096
    trace = json.loads((tmp_path / "trace.jsonl").read_text(encoding="utf-8"))
    assert trace["status"] == "success"
    assert trace["response_format"] == response_format
    assert trace["require_complete"] is True
    assert "error_category" not in trace
    assert "YWJj" not in json.dumps(trace)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['{"score":', '{"score": 4}'])
async def test_length_is_rejected_even_if_body_looks_like_complete_json(tmp_path, content):
    path = tmp_path / "trace.jsonl"
    client, requests = _client(content=content, finish_reason="length", trace_path=path)
    with pytest.raises(JudgeOutputTruncatedError, match="截断") as raised:
        await client.complete("system", "user", require_complete=True)
    error = raised.value
    assert isinstance(error, JudgeOutputParseError)
    assert error.raw_output == content
    assert error.repair_output == ""
    assert error.error_category == "truncated"
    assert error.finish_reason == "length"
    assert error.judge == "judge_2"
    assert error.model == "test-model"
    assert len(requests) == 1
    records = path.read_text(encoding="utf-8").splitlines()
    assert len(records) == 1
    trace = json.loads(records[0])
    assert trace["status"] == "error"
    assert trace["error_category"] == "truncated"
    assert trace["error_type"] == "JudgeOutputTruncatedError"
    assert trace["finish_reason"] == "length"
    assert trace["llm_rounds"][0]["content"] == content
    assert trace["llm_rounds"][0]["finish_reason"] == "length"
    merged = merge_trace_web_result(trace, {"error_category": "evaluation_failed"})
    assert merged["call_error_category"] == "truncated"
    assert merged["model_raw_output"] == content


@pytest.mark.asyncio
async def test_legacy_call_retains_previous_finish_reason_behavior():
    client, _ = _client(content='{"score":', finish_reason="length")
    assert await client.complete("system", "legacy") == '{"score":'


@pytest.mark.asyncio
async def test_complete_response_is_returned_with_guard_enabled():
    client, _ = _client(finish_reason="stop")
    assert await client.complete("system", "user", require_complete=True) == '{"result": "ok"}'


def test_parse_error_constructor_remains_compatible():
    error = JudgeOutputParseError(
        "legacy error", raw_output="raw", repair_output="repair", judge="j", model="m",
    )
    assert error.error_category is None
    assert error.finish_reason is None


def _empty_length_stream_client(path):
    """Use the real stream/complete path, substituting only the network endpoint."""
    requests = []

    class Stream:
        async def __aiter__(self):
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                    finish_reason="length",
                )],
                model="test-model", usage=None,
            )

        async def close(self):
            pass

    async def create(**kwargs):
        requests.append(kwargs)
        return Stream()

    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="judge_2", model="test-model", max_attempts=5, retry_base_s=0)
    client.model = "test-model"
    client.persona = "测试裁判"
    client.trace_path = str(path)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, requests


@pytest.mark.asyncio
async def test_new_protocol_classifies_empty_length_without_retry_or_json_repair(tmp_path):
    from auto_eval.judges.compare_protocols import V1022_COMPARE_PROTOCOL_ID
    from auto_eval.judges.visual_compare_judge import VisualCompareJudge

    path = tmp_path / "empty-length.jsonl"
    client, requests = _empty_length_stream_client(path)

    async def repair(*args, **kwargs):
        pytest.fail("A truncated judgment must not enter JSON repair")

    client.repair_json = repair
    profile = VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))
    judge = VisualCompareJudge(client, profile, protocol=V1022_COMPARE_PROTOCOL_ID)
    with pytest.raises(JudgeOutputTruncatedError) as raised:
        await judge.evaluate(question="你好", answer1="你好", answer2="你好", product_count=2,
                             context1="产品1测试背景", context2="产品2测试背景")

    assert len(requests) == 1
    assert raised.value.raw_output == ""
    assert raised.value.repair_output == ""
    assert raised.value.finish_reason == "length"
    assert raised.value.error_category == "truncated"
    assert isinstance(raised.value.__cause__, ProviderStreamError)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error_type"] == "JudgeOutputTruncatedError"
    assert records[0]["error_category"] == "truncated"
    assert records[0]["finish_reason"] == "length"
    assert records[0]["llm_rounds"][0]["content"] == ""


@pytest.mark.asyncio
async def test_legacy_empty_length_remains_provider_error_and_is_not_retried(tmp_path):
    path = tmp_path / "legacy-empty-length.jsonl"
    client, requests = _empty_length_stream_client(path)
    with pytest.raises(ProviderStreamError) as raised:
        await client.complete("system", "legacy protocol")
    assert len(requests) == 1
    assert raised.value.retriable is False
    trace = json.loads(path.read_text(encoding="utf-8"))
    assert trace["status"] == "error"
    assert trace["error_type"] == "ProviderStreamError"
