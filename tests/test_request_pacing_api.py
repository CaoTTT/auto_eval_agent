import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.request_throttle import MODEL
from auto_eval.web import server


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [False, True])
async def test_reading_pacing_does_not_initialize_a_sender(monkeypatch, supported):
    judge = SimpleNamespace(
        model=MODEL if supported else "other-model",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="PRIVATE_KEY_NAME",
    )
    monkeypatch.setattr(server, "cfg", lambda: SimpleNamespace(judges=[judge]))
    loop = asyncio.get_running_loop()
    monkeypatch.delattr(loop, "_auto_eval_request_throttles", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/request-pacing")
    assert response.status_code == 200
    assert response.json() == {
        "enabled": supported,
        "active": False,
        "scope": "process_event_loop",
        "controller": None,
    }
    assert not hasattr(loop, "_auto_eval_request_throttles")
    assert "PRIVATE_KEY_NAME" not in response.text
    assert "dashscope.aliyuncs.com" not in response.text


@pytest.mark.asyncio
async def test_pacing_returns_shared_runtime_counts_without_credentials(monkeypatch):
    from auto_eval.request_throttle import RequestThrottle

    judge = SimpleNamespace(
        model=MODEL,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="PRIVATE_KEY_NAME",
    )
    monkeypatch.setattr(server, "cfg", lambda: SimpleNamespace(judges=[judge]))
    throttle = RequestThrottle(warmup_s=0)
    record = await throttle.acquire(1000)
    monkeypatch.setattr(
        asyncio.get_running_loop(), "_auto_eval_request_throttles", {MODEL: throttle}, raising=False
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app), base_url="http://test"
        ) as client:
            first = (await client.get("/api/request-pacing")).json()
            second = (await client.get("/api/request-pacing")).json()
        assert first["enabled"] and first["active"]
        counts = first["controller"]
        assert counts["inflight"] == 1
        assert counts["requests_last_second"] == counts["requests_last_minute"] == 1
        assert counts["reserved_tokens"] == 1000
        assert counts["hard_second_limit"] == 10
        assert second["controller"]["total_requests"] == counts["total_requests"]
        public_json = json.dumps(first)
        assert "PRIVATE_KEY_NAME" not in public_json
        assert "api_key" not in public_json
    finally:
        throttle.finish(record)
