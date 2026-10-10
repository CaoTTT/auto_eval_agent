import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from openai import APIStatusError
from PIL import Image
import pytest

from auto_eval.config import AppConfig, JudgeConfig, VisualExtractionConfig, VisualModeProfile
from auto_eval.image_limits import enforce_request_budget, request_budget, resolve_image_limits
from auto_eval.judge_profiles import configured_profile, legacy_runtime, model_profiles, resolve_new_runtime, restore_runtime
from auto_eval.judges.base import JudgeClient, merge_trace_web_result
from auto_eval.llm_stream import ProviderStreamError, is_retriable_llm_error, stream_chat_completion
from auto_eval.query_images import QueryImageError, check_request_budget
from auto_eval.observability import bind_chain_context
from auto_eval.request_errors import nonretryable_context_error, request_limit_diagnostics


def _status_error(status, body, message="provider error"):
    response = httpx.Response(status, request=httpx.Request("POST", "https://model.example/v1/chat/completions"),
                              json=body, headers={"x-request-id": "request-123"})
    return APIStatusError(message, response=response, body=body)


class _Stream:
    def __init__(self, error=None):
        self.error = error

    async def __aiter__(self):
        if self.error:
            yield SimpleNamespace(choices=[], error=self.error, usage=None, model="test")
        else:
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")], usage=None, model="test")

    async def close(self):
        pass


def _client(attempts):
    create = AsyncMock(side_effect=attempts)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_provider_context_error_stops_every_stream_retry(status):
    exc = _status_error(status, {"error": {"code": "context_length_exceeded", "message": "Input exceeds maximum context length"}})
    client, create = _client([exc, _Stream()])
    with pytest.raises(APIStatusError) as raised:
        await stream_chat_completion(client, {"model": "test", "messages": []}, max_attempts=4, retry_base_s=0)
    assert create.await_count == 1
    assert not is_retriable_llm_error(exc)
    assert raised.value.context_limit_error["origin"] == "provider"
    assert raised.value.context_limit_error["provider_status"] == status
    assert raised.value.context_limit_error["provider_request_id"] == "request-123"
    assert raised.value.context_limit_error["automatic_retry_skipped"] is True


async def test_context_error_never_falls_back_from_stream_usage():
    exc = _status_error(400, {"code": "context_length_exceeded", "message": "context_length_exceeded; stream_options"})
    client, create = _client([exc, _Stream()])
    with pytest.raises(APIStatusError):
        await stream_chat_completion(client, {"model": "test", "messages": []}, max_attempts=4, retry_base_s=0)
    assert create.await_count == 1


async def test_nested_stream_context_error_stops_even_with_rate_limit_status():
    client, create = _client([_Stream({"status": 429, "data": {"error": {
        "code": "input_token_limit_exceeded", "message": "input tokens too long"}}}), _Stream()])
    with pytest.raises(ProviderStreamError) as raised:
        await stream_chat_completion(client, {"model": "test", "messages": []}, max_attempts=3, retry_base_s=0)
    assert create.await_count == 1
    assert raised.value.context_limit_error["retry_allowed"] is False


async def test_token_per_minute_quota_remains_retriable():
    exc = _status_error(429, {"error": {"code": "rate_limit_exceeded", "message": "Input tokens exceed tokens per minute TPM quota"}})
    assert not nonretryable_context_error(exc)
    client, create = _client([exc, _Stream()])
    response = await stream_chat_completion(client, {"model": "test", "messages": []}, max_attempts=2, retry_base_s=0)
    assert create.await_count == 2
    assert response.choices[0].message.content == "ok"


@pytest.mark.parametrize("message", [
    "Range of input length should be [1, 129024]",
    "上下文长度超过模型上限",
    "输入 token 数量超限",
    "This model's maximum context length is 8192 tokens, however you requested 9000 tokens",
    "Input length is too long",
])
def test_common_context_messages(message):
    assert nonretryable_context_error(RuntimeError(message))


@pytest.mark.parametrize("message", [
    "max_output_tokens exceeds maximum output length",
    "finish_reason=length",
    "max_completion_tokens must be less than 8192",
    "tokens per minute limit exceeded",
    "连接超时",
])
def test_output_cutoff_and_quota_are_not_input_context_errors(message):
    assert not nonretryable_context_error(RuntimeError(message))


def test_sdk_context_code_is_provider_origin_and_payload_is_redacted():
    exc = _status_error(400, {"code": "context_length_exceeded", "message": "context exceeded data:image/png;base64,abc123 Bearer secret",
                              "request": {"api_key": "should-not-be-persisted"}})
    exc.code = "context_length_exceeded"
    report = request_limit_diagnostics(exc, model="test", request_budget_report={"report_id": "budget-1"})
    assert report["origin"] == "provider"
    assert report["code"] == "context_limit_exceeded"
    assert report["request_budget_report"]["report_id"] == "budget-1"
    saved = json.dumps(report)
    assert "abc123" not in saved and "Bearer secret" not in saved and "should-not-be-persisted" not in saved


def test_generic_payload_too_large_is_not_context_overflow():
    report = request_limit_diagnostics(_status_error(413, {"message": "Request body too large"}))
    assert report["code"] == "request_payload_bytes_exceeded"
    assert report["retry_allowed"] is False


def test_wrapped_context_error_keeps_provider_status_and_request_identity():
    source = _status_error(429, {"error": {"code": "context_length_exceeded", "message": "context too long"}})
    wrapped = RuntimeError("judge failed")
    wrapped.__cause__ = source
    report = request_limit_diagnostics(wrapped)
    assert report["origin"] == "provider"
    assert report["provider_status"] == 429
    assert report["provider_request_id"] == "request-123"
    assert report["provider_code"] == "context_length_exceeded"
    assert "context too long" in report["reason"]


def test_wrapped_local_error_uses_actual_failed_budget_instead_of_stale_item_report():
    source = QueryImageError("context_budget_exceeded", "local input overflow",
                             request_budget_report={"report_id": "actual", "blocking_reasons": ["context_budget_exceeded"]})
    wrapped = RuntimeError("preparation failed")
    wrapped.__cause__ = source
    report = request_limit_diagnostics(wrapped, request_budget_report={"report_id": "stale"})
    assert report["origin"] == "local_guardrail"
    assert report["request_budget_report"]["report_id"] == "actual"


def test_wrapped_image_count_error_is_not_misclassified_by_context_limit_explanation():
    source = QueryImageError("request_image_limit_exceeded", "图片数超限；输入上限 100；上下文上限 200",
                             request_budget_report={"blocking_reasons": ["request_image_count_exceeded"]})
    wrapped = RuntimeError("preparation failed")
    wrapped.__cause__ = source
    report = request_limit_diagnostics(wrapped)
    assert report is not None
    assert report["code"] == "request_image_limit_exceeded"
    assert not nonretryable_context_error(wrapped)


def test_http_status_error_payload_exposes_context_detection():
    response = httpx.Response(429, request=httpx.Request("POST", "https://model.example"),
                              json={"error": {"code": "context_length_exceeded"}})
    error = httpx.HTTPStatusError("429", request=response.request, response=response)
    assert nonretryable_context_error(error)
    assert request_limit_diagnostics(error)["retry_allowed"] is False


def _parts(count=1):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 96)).save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    return [{"type": "text", "text": "abc"}] + [{"type": "image_url", "image_url": {"url": url}} for _ in range(count)]


def _profile():
    return VisualModeProfile(extraction=VisualExtractionConfig(algorithm_version="test"))


def test_no_question_image_budget_checks_all_actual_views_and_declared_model_cap():
    profile = _profile()
    judge = JudgeConfig(name="test", model="custom-model", max_input_tokens=500, context_window=1000)
    limits = resolve_image_limits(judge, profile)
    report = request_budget("system", _parts(2), [{"product_no": 1, "image_role": "product_answer"},
                                               {"product_no": 1, "image_role": "supplementary_video", "time": 2.5}], limits, profile)
    assert report["actual_image_count"] == 2
    assert report["estimated_input_tokens"] == 533
    assert report["effective_limits"]["max_input_tokens"] == 500
    assert report["model_declared_limits"]["context_window"] == 1000
    assert report["image_views"][1]["time"] == 2.5
    assert report["image_views"][1]["sha256"]
    assert report["token_estimation_is_billing"] is False
    with pytest.raises(QueryImageError) as raised:
        enforce_request_budget(report)
    assert raised.value.request_budget_report is report
    assert raised.value.context_limit_error["origin"] == "local_guardrail"
    assert raised.value.context_limit_error["retry_allowed"] is False
    assert "base64" not in json.dumps(report)


def test_query_budget_compatibility_also_preserves_failed_report():
    cfg = _profile().query_images
    cfg.max_input_tokens = 100
    with pytest.raises(QueryImageError) as raised:
        check_request_budget("system", _parts(), cfg)
    assert raised.value.code == "context_budget_exceeded"
    assert raised.value.request_budget_report["actual_image_count"] == 1


def test_boundary_and_image_count_errors_are_distinct():
    profile = _profile()
    profile.query_images.max_input_tokens = 533
    profile.long_screenshot.max_input_tokens = 533
    parts = _parts(2)
    limits = resolve_image_limits(SimpleNamespace(model="test"), profile)
    report = request_budget("system", parts, [], limits, profile)
    assert not report["blocked"]
    profile.query_images.max_request_images = 1
    report = request_budget("system", parts, [], limits, profile)
    with pytest.raises(QueryImageError) as raised:
        enforce_request_budget(report)
    assert raised.value.code == "request_image_limit_exceeded"
    assert not nonretryable_context_error(raised.value)


async def test_declared_judge_cap_blocks_before_model_or_repair_request():
    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="test", model="test", max_input_tokens=100)
    client.model = "test"
    client.trace_path = None
    client._llm_create_stream = AsyncMock()
    with pytest.raises(QueryImageError) as raised:
        await client._llm_create({"model": "test", "messages": [{"role": "user", "content": "x" * 101}]})
    assert client._llm_create_stream.await_count == 0
    assert raised.value.request_budget_report["estimated_input_tokens"] == 101


@pytest.mark.parametrize("declared_caps", [False, True])
async def test_rejected_repair_trace_preserves_actual_messages_and_its_own_budget(tmp_path, declared_caps):
    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="test", model="test", max_input_tokens=20000 if declared_caps else None)
    client.model = "test"
    client.trace_path = str(tmp_path / "judge_calls.jsonl")
    error = _status_error(400, {"error": {"code": "context_length_exceeded", "message": "Input exceeds maximum context length"}})
    client._llm_create_stream = AsyncMock(side_effect=error)
    malformed = '{"score": 1} data:image/png;base64,abc123'
    with bind_chain_context(task_id="task-1", request_id="request-1", item_id="case-1"):
        with pytest.raises(APIStatusError) as raised:
            await client.repair_json(malformed, round_no=2)
    assert client._llm_create_stream.await_count == 1
    actual_messages = client._llm_create_stream.call_args.args[0]["messages"]
    assert malformed in actual_messages[1]["content"]
    record = json.loads((tmp_path / "judge_calls.jsonl").read_text(encoding="utf-8"))
    assert record["task_id"] == "task-1"
    assert record["item_id"] == "case-1"
    assert record["request_id"] == "request-1"
    assert record["model"] == "test" and record["round"] == 2
    assert "score" in record["messages"][1]["content"]
    assert "abc123" not in json.dumps(record)
    assert record["context_limit_error"]["origin"] == "provider"
    assert record["context_limit_error"]["automatic_retry_skipped"] is True
    if declared_caps:
        report = raised.value.request_budget_report
        assert report["blocked"] is False
        assert record["request_budget_report"] == report
        assert raised.value.context_limit_error["request_budget_report"] == report
        merged = merge_trace_web_result(record, {"request_budget_report": {"report_id": "original-model-call"}})
        assert merged["call_request_budget_report"] == report
    else:
        assert record["request_budget_report"] is None


def test_declared_model_caps_are_selected_frozen_and_not_inherited_by_other_model():
    config = AppConfig(judges=[JudgeConfig(name="test", model="qwen3.5-397b-a17b",
                                         base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                                         max_input_tokens=12345, context_window=23456)])
    profiles = model_profiles(config)
    current = next(p for p in profiles if p.model == config.judges[0].model)
    sibling = next(p for p in profiles if p.model != config.judges[0].model)
    assert configured_profile(config, current).max_input_tokens == 12345
    assert configured_profile(config, sibling).max_input_tokens is None
    runtime, judges = resolve_new_runtime(config, {"judge_model_profile": current.id})
    assert runtime["judges"][0]["context_window"] == 23456
    config.judges[0].context_window = 34567
    assert restore_runtime(config, runtime)[0].context_window == judges[0].context_window == 23456


@pytest.mark.parametrize("historical_model", ["current-model", "older-model"])
def test_legacy_runtime_never_inherits_newly_configured_token_caps(historical_model):
    config = AppConfig(judges=[JudgeConfig(name="test", model="current-model",
                                         max_input_tokens=260096, context_window=262144)])
    task = SimpleNamespace(protocol_manifest={"judges": [{"name": "test", "model": historical_model}]},
                           options={"judges": ["test"]}, results=[])
    migrated = legacy_runtime(config, task)["judges"][0]
    assert migrated["model"] == historical_model
    assert migrated["max_input_tokens"] is None
    assert migrated["context_window"] is None


def test_legacy_runtime_preserves_only_token_caps_actually_recorded_in_history():
    config = AppConfig(judges=[JudgeConfig(name="test", model="current-model",
                                         max_input_tokens=260096, context_window=262144)])
    task = SimpleNamespace(protocol_manifest={"judges": [{"name": "test", "model": "older-model",
                           "context_window": 131072}]}, options={"judges": ["test"]}, results=[])
    migrated = legacy_runtime(config, task)["judges"][0]
    assert migrated["max_input_tokens"] is None
    assert migrated["context_window"] == 131072
