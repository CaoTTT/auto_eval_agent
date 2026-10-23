import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.llm_stream import ProviderStreamError, stream_chat_completion


def _chunk(content=None, *, finish=None, tool_calls=None, usage=None, error=None, reasoning=None):
    choices = []
    if content is not None or finish is not None or tool_calls or reasoning is not None:
        delta = SimpleNamespace(content=content, tool_calls=tool_calls)
        if reasoning is not None:
            delta.reasoning_content = reasoning
        choices = [
            SimpleNamespace(
                delta=delta,
                finish_reason=finish,
            )
        ]
    chunk = SimpleNamespace(
        choices=choices,
        usage=usage,
        model="fake-model",
    )
    if error is not None:
        chunk.error = error
    return chunk


def _tool_chunk(index, *, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeStream:
    def __init__(self, events, delay=0):
        self.events = events
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for event in self.events:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self):
        self.closed = True


class FakeCompletions:
    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        attempt = self.attempts.pop(0)
        if isinstance(attempt, BaseException):
            raise attempt
        return FakeStream(attempt)


def _client(attempts):
    completions = FakeCompletions(attempts)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    ), completions


@pytest.mark.asyncio
async def test_stream_aggregates_content_usage_and_tool_calls():
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4)
    client, completions = _client(
        [[
            _chunk("你"),
            _chunk("好"),
            _chunk(
                tool_calls=[
                    _tool_chunk(0, call_id="call-1", name="web_", arguments='{"q":')
                ]
            ),
            _chunk(
                finish="tool_calls",
                tool_calls=[_tool_chunk(0, name="search", arguments='"x"}')],
            ),
            _chunk(usage=usage),
        ]]
    )
    callback_chunks = []

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        callback=callback_chunks.append,
        max_attempts=1,
    )

    request = completions.requests[0]
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert response.choices[0].message.content == "你好"
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage is usage
    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.id == "call-1"
    assert tool_call.function.name == "web_search"
    assert tool_call.function.arguments == '{"q":"x"}'
    assert callback_chunks == ["你", "好"]


@pytest.mark.asyncio
async def test_stream_retry_discards_partial_callback_output():
    client, completions = _client(
        [
            [_chunk("重复内容"), httpx.RemoteProtocolError("peer closed")],
            [_chunk("完"), _chunk("整", finish="stop")],
        ]
    )
    callback_chunks = []

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        callback=callback_chunks.append,
        max_attempts=2,
        retry_base_s=0,
    )

    assert len(completions.requests) == 2
    assert response.choices[0].message.content == "完整"
    assert callback_chunks == ["完", "整"]


@pytest.mark.asyncio
async def test_stream_total_timeout():
    completions = FakeCompletions([])

    async def create(**kwargs):
        completions.requests.append(kwargs)
        return FakeStream([_chunk("太慢")], delay=0.05)

    completions.create = create
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(asyncio.TimeoutError):
        await stream_chat_completion(
            client,
            {"model": "fake-model", "messages": []},
            total_timeout_s=0.01,
            max_attempts=1,
        )


@pytest.mark.asyncio
async def test_stream_retries_structured_error_chunk(monkeypatch):
    events = []
    monkeypatch.setattr(
        "auto_eval.llm_stream.log_event",
        lambda _module, event, **_kwargs: events.append(event),
    )
    client, completions = _client(
        [
            [_chunk(error={"message": "request aborted", "code": "aborted"})],
            [_chunk("分类标签", finish="stop")],
        ]
    )

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        max_attempts=2,
        retry_base_s=0,
    )

    assert len(completions.requests) == 2
    assert response.choices[0].message.content == "分类标签"
    assert "服务端返回错误，准备重试" in events
    assert "流式调用成功" not in events
    assert "重试成功" in events


@pytest.mark.asyncio
async def test_stream_retries_aborted_finish_reason():
    client, completions = _client(
        [
            [_chunk(finish="aborted")],
            [_chunk("有效回答", finish="stop")],
        ]
    )

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        max_attempts=2,
        retry_base_s=0,
    )

    assert len(completions.requests) == 2
    assert response.choices[0].message.content == "有效回答"


@pytest.mark.asyncio
async def test_stream_retries_empty_response():
    client, completions = _client(
        [
            [_chunk(finish="stop")],
            [_chunk("有效回答", finish="stop")],
        ]
    )

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": []},
        max_attempts=2,
        retry_base_s=0,
    )

    assert len(completions.requests) == 2
    assert response.choices[0].message.content == "有效回答"


@pytest.mark.asyncio
async def test_stream_does_not_retry_empty_length_response():
    client, completions = _client([
        [_chunk(finish="length")],
        [_chunk("不应通过重试补评分", finish="stop")],
    ])

    with pytest.raises(ProviderStreamError) as raised:
        await stream_chat_completion(
            client,
            {"model": "fake-model", "messages": []},
            max_attempts=5,
            retry_base_s=0,
        )

    assert raised.value.retriable is False
    assert raised.value.body == {"finish_reason": "length", "content": ""}
    assert len(completions.requests) == 1


@pytest.mark.asyncio
async def test_stream_keeps_nonempty_length_response_for_callers_to_handle():
    client, completions = _client([[_chunk('{"score":', finish="length")]])
    response = await stream_chat_completion(
        client, {"model": "fake-model", "messages": []}, max_attempts=5,
    )
    assert response.choices[0].finish_reason == "length"
    assert response.choices[0].message.content == '{"score":'
    assert len(completions.requests) == 1


@pytest.mark.asyncio
async def test_stream_does_not_retry_blocked_error_chunk():
    client, completions = _client(
        [
            [_chunk(error={"message": "content blocked", "code": "content_filter"})],
            [_chunk("不应调用", finish="stop")],
        ]
    )

    with pytest.raises(ProviderStreamError, match="content blocked") as exc_info:
        await stream_chat_completion(
            client,
            {"model": "fake-model", "messages": []},
            max_attempts=2,
            retry_base_s=0,
        )

    assert exc_info.value.retriable is False
    assert len(completions.requests) == 1


@pytest.mark.asyncio
async def test_stream_keeps_reasoning_out_of_answers_and_retry(monkeypatch):
    events = []
    monkeypatch.setattr(
        "auto_eval.llm_stream.log_event",
        lambda _module, event, **_kwargs: events.append(event),
    )
    client, completions = _client([
        [_chunk(reasoning="private first thought"), _chunk("discard"),
         httpx.RemoteProtocolError("closed")],
        [_chunk(reasoning="private second thought"), _chunk('{"score":'), _chunk("4}", finish="stop")],
    ])
    output = []

    response = await stream_chat_completion(
        client,
        {"model": "fake-model", "messages": [], "extra_body": {"enable_thinking": True}},
        callback=output.append,
        max_attempts=2,
        retry_base_s=0,
    )

    assert len(completions.requests) == 2
    assert response.choices[0].message.content == '{"score":4}'
    assert output == ['{"score":', '4}']
    assert response.stream_stats["思考chunk数"] == 1
    assert response.stream_stats["首响应耗时"] <= response.stream_stats["首答案耗时"]
    assert any(event == "模型思考中" for event in events)
    assert "private" not in json.dumps(events, ensure_ascii=False, default=str)
    assert "private" not in repr(response)


@pytest.mark.asyncio
async def test_truncated_reasoning_only_is_not_blindly_retried():
    client, completions = _client([
        [_chunk(reasoning="private", finish="length")],
    ])

    with pytest.raises(ProviderStreamError, match="仅返回思考内容") as caught:
        await stream_chat_completion(
            client,
            {"model": "fake-model", "messages": []},
            max_attempts=4,
        )

    assert not caught.value.retriable
    assert "private" not in str(caught.value.body)
    assert len(completions.requests) == 1
