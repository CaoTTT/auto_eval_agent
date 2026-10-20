"""Exercise configured sampling through the real SDK without network calls."""
import json

import httpx
import pytest
from openai import AsyncOpenAI

from auto_eval.config import JudgeConfig
from auto_eval.judges.base import JudgeClient


SAMPLING = {
    "temperature": 0.0,
    "top_p": 1.0,
    "top_k": 1,
    "seed": 0,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "max_tokens": 2048,
    "n": 1,
    "enable_thinking": False,
}
API_KEY = "sk-offline-sampling-test-secret"


def _stream_response():
    chunks = [
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "judge-fixed-version",
            "system_fingerprint": "fp_test_backend",
            "choices": [{"index": 0, "delta": {"content": "{}"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "judge-fixed-version",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    return httpx.Response(
        200, text=body + "data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _client(monkeypatch, handler, *, trace_path=None, **settings):
    monkeypatch.setenv("SAMPLING_TEST_API_KEY", API_KEY)

    def build_client(**kwargs):
        return AsyncOpenAI(
            base_url=kwargs["base_url"], api_key=kwargs["api_key"], max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr("auto_eval.judges.base.build_openai_client", build_client)
    cfg = JudgeConfig(
        name="test", model="judge-alias", base_url="https://offline.invalid/v1",
        api_key_env="SAMPLING_TEST_API_KEY", retry_base_s=0, retry_max_s=0,
        **settings,
    )
    return JudgeClient(cfg, trace_path=str(trace_path) if trace_path else None)


@pytest.mark.asyncio
async def test_sampling_reaches_http_for_text_images_and_repairs_and_is_auditable(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        requests.append(json.loads(request.content))
        return _stream_response()

    trace_path = tmp_path / "judge_calls.jsonl"
    client = _client(monkeypatch, handler, trace_path=trace_path,
                     vl_high_resolution_images=True, **SAMPLING)
    try:
        assert await client.complete("system", "question") == "{}"
        assert await client.complete("system", "question", user_images=[
            "data:image/png;base64,YWJj",
        ], user_image_refs=["source.png"]) == "{}"
        assert await client.repair_json('{"broken":') == "{}"
    finally:
        await client.aclose()

    assert len(requests) == 3
    for request in requests:
        assert {key: request[key] for key in SAMPLING} == SAMPLING
        assert request["enable_thinking"] is False
        assert request["stream"] is True
        # The provider extension must be flattened by the real SDK, never sent
        # as an unsupported create(top_k=...) argument or an extra_body object.
        assert "extra_body" not in request
    assert requests[1]["vl_high_resolution_images"] is True
    assert "vl_high_resolution_images" not in requests[0]
    assert "vl_high_resolution_images" not in requests[2]
    assert isinstance(requests[2]["messages"][1]["content"], str)

    trace_text = trace_path.read_text(encoding="utf-8")
    assert API_KEY not in trace_text
    assert "data:image" not in trace_text and "YWJj" not in trace_text
    records = [json.loads(line) for line in trace_text.splitlines()]
    assert len(records) == 3
    for request, record in zip(requests, records):
        expected = {"model": "judge-alias", **SAMPLING}
        if "vl_high_resolution_images" in request:
            expected["vl_high_resolution_images"] = True
        assert record["inference_parameters"] == expected
        assert record["llm_rounds"][0]["response_model"] == "judge-fixed-version"
        assert record["llm_rounds"][0]["system_fingerprint"] == "fp_test_backend"
    assert records[2]["purpose"] == "json_repair"


@pytest.mark.asyncio
async def test_stream_options_fallback_then_transient_retry_preserve_sampling(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {
                "message": "stream_options is unsupported", "type": "invalid_request_error",
            }})
        if len(requests) == 2:
            return httpx.Response(503, json={"error": {
                "message": "temporarily unavailable", "type": "server_error",
            }})
        return _stream_response()

    client = _client(monkeypatch, handler, max_attempts=2, **SAMPLING)
    callbacks = []
    try:
        assert await client.complete("system", "question", stream_callback=callbacks.append) == "{}"
    finally:
        await client.aclose()

    assert len(requests) == 3
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in requests[1] and "stream_options" not in requests[2]
    for request in requests:
        assert {key: request[key] for key in SAMPLING} == SAMPLING
    assert {key: value for key, value in requests[0].items() if key != "stream_options"} == requests[1]
    assert requests[1] == requests[2]
    assert callbacks == ["{}"]


@pytest.mark.asyncio
async def test_unconfigured_optional_parameters_are_omitted_for_gateway_compatibility(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _stream_response()

    optional = {key: None for key in SAMPLING if key != "temperature"}
    client = _client(monkeypatch, handler, **optional)
    try:
        await client.complete("system", "question")
        await client.repair_json("broken")
    finally:
        await client.aclose()

    assert len(requests) == 2
    for request in requests:
        assert request["temperature"] == 0
        assert not optional.keys() & request.keys()
        assert "extra_body" not in request


@pytest.mark.asyncio
async def test_repair_overrides_temperature_without_losing_configured_sampling(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _stream_response()

    client = _client(monkeypatch, handler, **{**SAMPLING, "temperature": 0.7})
    try:
        await client.complete("system", "question")
        await client.repair_json("broken")
    finally:
        await client.aclose()

    assert requests[0]["temperature"] == 0.7
    assert {key: requests[1][key] for key in SAMPLING} == SAMPLING
