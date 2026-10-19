"""Request-pacing API exposes global limiter stats without credentials."""
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.web import server


@pytest.mark.asyncio
async def test_request_pacing_returns_limiter_stats_without_credentials(monkeypatch):
    model_stats = {"limit": 5, "running": 2, "queued": 1, "max_in_flight": 40}
    pipeline_stats = {"limit": 50, "running": 8, "queued": 2}

    monkeypatch.setattr(server, "MODEL_LIMITER", SimpleNamespace(stats=lambda: model_stats))
    monkeypatch.setattr(server, "PIPELINE_LIMITER", SimpleNamespace(stats=lambda: pipeline_stats))
    monkeypatch.setattr(
        server,
        "get_settings",
        lambda: SimpleNamespace(
            concurrency=5, max_in_flight=40, waiting_capacity=10, eval_timeout_s=900.0,
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        response = await client.get("/api/request-pacing")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["active"] is True
    assert payload["model_limiter"] == model_stats
    assert payload["pipeline_limiter"] == pipeline_stats
    assert payload["settings"]["concurrency"] == 5
    assert payload["settings"]["max_in_flight"] == 40
    assert payload["settings"]["eval_timeout_s"] == 900.0
    assert "api_key" not in response.text
    assert "dashscope" not in response.text
