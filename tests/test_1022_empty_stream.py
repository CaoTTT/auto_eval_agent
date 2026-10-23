"""Exercise the real stream collector; replace only its network endpoint."""
import json
from types import SimpleNamespace

import pytest

from auto_eval.config import JudgeConfig
from auto_eval.judges.base import (
    JudgeClient, JudgeOutputEmptyError, JudgeOutputTruncatedError,
)
from auto_eval.llm_stream import ProviderStreamError


def chunk(*, finish=None, content=None, reasoning=None, error=None):
    value = SimpleNamespace(choices=[SimpleNamespace(
        delta=SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None),
        finish_reason=finish,
    )], model="test-model", usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2))
    if error is not None:
        value.error = error
    return value


def client_with_streams(path, attempts):
    streams, requests = [], []

    class Stream:
        closed = False

        def __init__(self, events):
            self.events = events

        async def __aiter__(self):
            for event in self.events:
                yield event

        async def close(self):
            self.closed = True

    async def create(**kwargs):
        requests.append(kwargs)
        stream = Stream(attempts[len(requests) - 1])
        streams.append(stream)
        return stream

    client = object.__new__(JudgeClient)
    client.cfg = JudgeConfig(name="judge_2", model="test-model", max_attempts=1, retry_base_s=0)
    client.model, client.persona, client.trace_path = "test-model", "test judge", str(path)
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, requests, streams


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", None])
@pytest.mark.parametrize("reasoning", [None, "private internal reasoning"])
async def test_actual_empty_stream_is_classified_and_audited_once(tmp_path, finish, reasoning):
    path = tmp_path / "empty.jsonl"
    client, requests, streams = client_with_streams(path, [[chunk(finish=finish, reasoning=reasoning)]])
    with pytest.raises(JudgeOutputEmptyError) as caught:
        await client.complete("system", "user", require_complete=True)
    error = caught.value
    assert error.raw_output == error.repair_output == ""
    assert error.error_category == "empty_response" and error.finish_reason == finish
    assert error.retriable is True
    assert isinstance(error.__cause__, ProviderStreamError)
    assert error.structural_diagnostics
    assert len(requests) == 1 and streams[0].closed
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["error_type"] == "JudgeOutputEmptyError"
    assert record["error_category"] == "empty_response" and record["status"] == "error"
    assert record["finish_reason"] == finish
    assert record["llm_rounds"][0]["content"] == ""
    assert record["llm_rounds"][0]["usage"]["completion_tokens"] == 2
    assert record["llm_rounds"][0]["stream_stats"]["思考chunk数"] == int(reasoning is not None)
    assert "private internal reasoning" not in path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_retriable_classification_allows_a_subsequent_complete_call(tmp_path):
    client, requests, streams = client_with_streams(tmp_path / "retry.jsonl", [
        [chunk(finish="stop")], [chunk(content='{"score":4}', finish="stop")],
    ])
    with pytest.raises(JudgeOutputEmptyError) as caught:
        await client.complete("system", "user", require_complete=True)
    assert caught.value.retriable
    assert await client.complete("system", "user", require_complete=True) == '{"score":4}'
    assert len(requests) == 2 and all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_actual_empty_length_still_uses_nonretriable_truncation(tmp_path):
    client, requests, _ = client_with_streams(tmp_path / "length.jsonl", [[chunk(finish="length")]])
    with pytest.raises(JudgeOutputTruncatedError) as caught:
        await client.complete("system", "user", require_complete=True)
    assert caught.value.error_category == "truncated"
    assert not getattr(caught.value, "retriable", False)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["content_filter", "safety", "aborted", "tool_calls"])
async def test_other_provider_finish_failures_are_not_reclassified(tmp_path, finish):
    client, _, _ = client_with_streams(tmp_path / "provider.jsonl", [[chunk(finish=finish)]])
    with pytest.raises(ProviderStreamError):
        await client.complete("system", "user", require_complete=True)
    record = json.loads((tmp_path / "provider.jsonl").read_text(encoding="utf-8"))
    assert record["error_type"] == "ProviderStreamError"


@pytest.mark.asyncio
async def test_error_chunk_with_empty_content_is_still_a_provider_error(tmp_path):
    client, _, _ = client_with_streams(tmp_path / "chunk-error.jsonl", [[chunk(error={
        "code": "invalid_request", "message": "provider rejected input", "content": "", "finish_reason": "stop",
    })]])
    with pytest.raises(ProviderStreamError, match="provider rejected input"):
        await client.complete("system", "user", require_complete=True)


@pytest.mark.asyncio
async def test_legacy_complete_retains_original_empty_stream_error(tmp_path):
    client, _, _ = client_with_streams(tmp_path / "legacy.jsonl", [[chunk(finish="stop")]])
    with pytest.raises(ProviderStreamError):
        await client.complete("system", "legacy")
    record = json.loads((tmp_path / "legacy.jsonl").read_text(encoding="utf-8"))
    assert record["error_type"] == "ProviderStreamError"
