"""Exercise paced SDK calls over real local HTTP/1.1 sockets, never a model API."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest

from auto_eval.llm_stream import build_openai_client, stream_chat_completion
from auto_eval.request_throttle import MODEL, RequestThrottle
from auto_eval.request_transport import HeaderAdmission, header_admission, install_header_trace


@dataclass
class ReceivedRequest:
    index: int
    arrived: float
    path: str
    headers: dict[str, str]
    body: dict


class LoopbackProvider:
    """Small HTTP/1.1 peer with controllable responses and socket cleanup."""

    def __init__(self, respond, header_idle_timeout=None):
        self.respond = respond
        self.header_idle_timeout = header_idle_timeout
        self.connections = 0
        self.idle_connections_closed = 0
        self.requests: list[ReceivedRequest] = []
        self.changed = asyncio.Event()
        self.tasks = set()
        self.writers = set()
        self.failures = []
        self.active = 0
        self.peak = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/v1"
        return self

    async def __aexit__(self, *_):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        for writer in list(self.writers):
            writer.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not self.failures, self.failures

    async def _handle(self, reader, writer):
        self.connections += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        counted = False
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), self.header_idle_timeout,
                )
            except asyncio.TimeoutError:
                self.idle_connections_closed += 1
                return
            arrived = time.monotonic()
            lines = raw.decode("latin1").split("\r\n")
            headers = dict(line.split(": ", 1) for line in lines[1:] if line)
            headers = {name.lower(): value for name, value in headers.items()}
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            request = ReceivedRequest(
                len(self.requests), arrived, lines[0].split()[1], headers,
                json.loads(body) if body else {},
            )
            self.requests.append(request)
            self.active += 1
            counted = True
            self.peak = max(self.peak, self.active)
            self.changed.set()
            await self.respond(request, writer)
        except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError):
            pass  # Cancellation and closed sockets are expected in cancellation tests.
        except Exception as exc:
            self.failures.append(exc)
        finally:
            if counted:
                self.active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, asyncio.CancelledError):
                pass
            self.writers.discard(writer)
            self.tasks.discard(task)

    async def wait_requests(self, count, timeout=10):
        async def wait():
            while len(self.requests) < count:
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(wait(), timeout)


async def send_json(writer, status=200, body=None):
    data = json.dumps(body or {"ok": True}).encode()
    writer.write(
        f"HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data
    )
    await writer.drain()


async def send_sse(writer, hold=None):
    def chunk(choices, usage=None):
        data = {"id": "local-test", "object": "chat.completion.chunk", "created": 0,
                "model": MODEL, "choices": choices}
        if usage is not None:
            data["usage"] = usage
        return b"data: " + json.dumps(data).encode() + b"\n\n"

    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                 b"Connection: close\r\n\r\n")
    writer.write(chunk([{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}]))
    await writer.drain()
    if hold is not None:
        await hold.wait()
    writer.write(chunk([{"index": 0, "delta": {}, "finish_reason": "stop"}]))
    writer.write(chunk([], {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}))
    writer.write(b"data: [DONE]\n\n")
    await writer.drain()


class ObservedThrottle(RequestThrottle):
    """Observe admissions; tiny known test payloads do not stress Token limits."""

    def __init__(self, **kwargs):
        super().__init__(rpm=540, tpm=900_000, warmup_s=0, **kwargs)
        self.admissions = []
        self.admitted = asyncio.Event()

    def estimate(self, input_tokens, kind="text"):
        return 64

    async def acquire(self, *args, **kwargs):
        record = await super().acquire(*args, **kwargs)
        self.admissions.append(record)
        self.admitted.set()
        return record


@pytest.fixture(autouse=True)
def local_proxy_bypass(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def sdk_client(provider, key="test-key", pool_size=None):
    client = build_openai_client(base_url=provider.url, api_key=key,
                                 connect_timeout_s=15, read_timeout_s=15)
    if pool_size is not None:
        # Configure a deliberately undersized real pool without replacing transport.
        client._client._transport._pool._max_connections = pool_size
    return client


async def complete(client, throttle, **options):
    return await stream_chat_completion(
        client, {"model": MODEL, "messages": [{"role": "user", "content": "test"}]},
        throttle=throttle, total_timeout_s=options.pop("total_timeout_s", 10),
        max_attempts=options.pop("max_attempts", 1), retry_base_s=0, retry_max_s=0,
        **options,
    )


def assert_second_window(times):
    ordered = sorted(times)
    for start in ordered:
        assert sum(start <= sent <= start + 1 for sent in ordered) <= 9


@pytest.mark.asyncio
async def test_real_sdk_client_without_header_gate_is_rejected_before_network():
    from openai import AsyncOpenAI

    async def respond(request, writer):
        await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = AsyncOpenAI(base_url=provider.url, api_key="test-key", max_retries=0,
                             http_client=httpx.AsyncClient(trust_env=False))
        throttle = ObservedThrottle()
        try:
            with pytest.raises(ValueError, match="build_openai_client"):
                await complete(client, throttle)
            assert not provider.requests
            assert not throttle.admissions
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pool_waits_before_admission_and_released_queue_stays_paced():
    release = asyncio.Event()

    async def respond(request, writer):
        if request.path.endswith("/blocker"):
            await release.wait()
            await send_json(writer)
        else:
            await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider, pool_size=1)
        throttle = ObservedThrottle()
        blocker = asyncio.create_task(client._client.get(provider.url + "/blocker"))
        jobs = []
        try:
            await provider.wait_requests(1)
            jobs = [asyncio.create_task(complete(client, throttle)) for _ in range(12)]
            await asyncio.sleep(.25)
            assert len(provider.requests) == 1
            assert not throttle.admissions, "Pool-blocked requests must not hold send permits"
            release.set()
            await blocker
            responses = await asyncio.wait_for(asyncio.gather(*jobs), 10)
            assert all(response.choices[0].message.content == "ok" for response in responses)
            sends = [request.arrived for request in provider.requests[1:]]
            assert len(sends) == len(throttle.admissions) == 12
            assert_second_window(sends)
            assert sends[-1] - sends[0] >= 1
        finally:
            release.set()
            for job in [blocker, *jobs]:
                job.cancel()
            await asyncio.gather(blocker, *jobs, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_two_sdk_keys_share_gate_while_slow_streams_overlap():
    finish_streams = asyncio.Event()

    async def respond(request, writer):
        await send_sse(writer, finish_streams)

    async with LoopbackProvider(respond) as provider:
        clients = [sdk_client(provider, "key-a"), sdk_client(provider, "key-b")]
        throttle = ObservedThrottle()
        jobs = [asyncio.create_task(complete(clients[index % 2], throttle)) for index in range(12)]
        try:
            await provider.wait_requests(12)
            assert all(not job.done() for job in jobs)
            assert provider.peak == 12, "The gate must not serialize full response streams"
            assert {request.headers["authorization"] for request in provider.requests} == {
                "Bearer key-a", "Bearer key-b",
            }
            assert len(throttle.admissions) == 12
            assert_second_window([request.arrived for request in provider.requests])
            finish_streams.set()
            responses = await asyncio.wait_for(asyncio.gather(*jobs), 5)
            assert all(response.choices[0].message.content == "ok" for response in responses)
        finally:
            finish_streams.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await asyncio.gather(*(client.close() for client in clients))


@pytest.mark.asyncio
async def test_backlog_waits_before_connect_so_gateway_idle_timeout_does_not_trigger_retries():
    async def respond(request, writer):
        await send_sse(writer)

    async with LoopbackProvider(respond, header_idle_timeout=.1) as provider:
        client = sdk_client(provider)
        throttle = RequestThrottle(warmup_s=0)
        # An earlier task's shared cooldown must not open idle sockets for the
        # new backlog. Use real estimates (initially >8K tokens per request).
        throttle._cooldown_until = time.monotonic() + .25
        jobs = [asyncio.create_task(complete(client, throttle)) for _ in range(6)]
        try:
            responses = await asyncio.wait_for(asyncio.gather(*jobs), 10)
            assert all(response.choices[0].message.content == "ok" for response in responses)
            assert provider.connections == len(provider.requests) == 6
            assert provider.idle_connections_closed == 0
            assert throttle.snapshot()["total_requests"] == 6
            assert_second_window([request.arrived for request in provider.requests])
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_usage_fallback_and_429_retry_each_reenter_real_header_gate():
    async def respond(request, writer):
        if request.index == 0:
            await send_json(writer, 400, {"error": {
                "message": "stream_options include_usage is unsupported", "type": "invalid_request_error",
            }})
        elif request.index == 1:
            await send_json(writer, 429, {"error": {
                "message": "limit_requests", "type": "rate_limit_error", "code": "limit_requests",
            }})
        else:
            await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        throttle = ObservedThrottle()
        try:
            response = await asyncio.wait_for(
                complete(client, throttle, max_attempts=2, total_timeout_s=1), 15,
            )
            assert response.choices[0].message.content == "ok"
            assert len(provider.requests) == len(throttle.admissions) == 3
            assert provider.requests[0].body["stream_options"] == {"include_usage": True}
            assert all("stream_options" not in request.body for request in provider.requests[1:])
            assert provider.requests[2].arrived - provider.requests[1].arrived >= 5
            assert_second_window([request.arrived for request in provider.requests])
            assert all(request.headers["x-dashscope-wait-timeout"] == "30" for request in provider.requests)
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_redirect_reenters_gate_once_without_wrapping_trace_recursively():
    async def respond(request, writer):
        if request.index == 0:
            writer.write(b"HTTP/1.1 307 Temporary Redirect\r\n"
                         b"Location: /v1/redirected\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
        else:
            await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider, pool_size=1)
        throttle = ObservedThrottle(max_inflight=1)
        try:
            response = await asyncio.wait_for(complete(client, throttle), 5)
            assert response.choices[0].message.content == "ok"
            assert len(provider.requests) == len(throttle.admissions) == 2
            assert provider.requests[1].path == "/v1/redirected"
            assert provider.requests[0].body == provider.requests[1].body
            assert not throttle.dispatch_lock.locked()
            assert throttle.snapshot()["inflight"] == 0
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_dispatch_lock_or_inflight_slot():
    hold_first = asyncio.Event()
    request_queued = asyncio.Event()

    async def respond(request, writer):
        await send_sse(writer, hold_first if request.index == 0 else None)

    async def observe_request(request):
        request_queued.set()

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        client._client.event_hooks["request"].insert(0, observe_request)
        throttle = ObservedThrottle(max_inflight=1)
        first = asyncio.create_task(complete(client, throttle))
        waiting = None
        try:
            await provider.wait_requests(1)
            request_queued.clear()
            waiting = asyncio.create_task(complete(client, throttle))
            await asyncio.wait_for(request_queued.wait(), 5)
            await asyncio.sleep(.05)
            assert not waiting.done()
            assert len(throttle.admissions) == 1
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            hold_first.set()
            await first
            response = await asyncio.wait_for(complete(client, throttle), 5)
            assert response.choices[0].message.content == "ok"
            assert len(provider.requests) == len(throttle.admissions) == 2
            assert not throttle.dispatch_lock.locked()
        finally:
            hold_first.set()
            first.cancel()
            if waiting is not None:
                waiting.cancel()
            await asyncio.gather(first, *([waiting] if waiting is not None else []), return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_prior_trace_pause_does_not_grant_permits_before_dispatch():
    resume = asyncio.Event()
    first_waiting = asyncio.Event()

    async def pause_before_headers(request):
        async def prior_trace(name, info):
            if name == "http11.send_request_headers.started":
                first_waiting.set()
                await resume.wait()
        request.extensions["trace"] = prior_trace

    async def respond(request, writer):
        await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        client._client.event_hooks["request"].insert(0, pause_before_headers)
        throttle = ObservedThrottle()
        jobs = [asyncio.create_task(complete(client, throttle)) for _ in range(12)]
        try:
            await asyncio.wait_for(first_waiting.wait(), 5)
            assert not provider.requests
            assert not throttle.admissions
            resume.set()
            await asyncio.wait_for(asyncio.gather(*jobs), 10)
            assert len(provider.requests) == len(throttle.admissions) == 12
            assert_second_window([request.arrived for request in provider.requests])
        finally:
            resume.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_paused_socket_header_write_does_not_accumulate_old_permits(monkeypatch):
    from httpcore._backends.anyio import AnyIOStream

    first_write_waiting = asyncio.Event()
    resume_write = asyncio.Event()
    writes_started = []
    original_write = AnyIOStream.write

    async def paused_write(stream, buffer, timeout=None):
        if buffer.startswith(b"POST "):
            writes_started.append(time.monotonic())
            if len(writes_started) == 1:
                first_write_waiting.set()
                await resume_write.wait()
        await original_write(stream, buffer, timeout)

    monkeypatch.setattr(AnyIOStream, "write", paused_write)

    async def respond(request, writer):
        await send_sse(writer)

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        throttle = ObservedThrottle()
        jobs = [asyncio.create_task(complete(client, throttle)) for _ in range(12)]
        try:
            await asyncio.wait_for(first_write_waiting.wait(), 5)
            await asyncio.sleep(.35)
            assert not provider.requests
            assert len(writes_started) == len(throttle.admissions) == 1
            resume_write.set()
            await asyncio.wait_for(asyncio.gather(*jobs), 10)
            assert len(provider.requests) == len(throttle.admissions) == 12
            assert_second_window([request.arrived for request in provider.requests])
            assert not throttle.dispatch_lock.locked()
        finally:
            resume_write.set()
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_connect_trace_is_ignored_without_sending_to_an_external_proxy():
    # CONNECT is a protocol trace event generated by httpcore inside HTTPS proxy
    # setup. Exercise the installed callback without requiring a TLS certificate.
    throttle = ObservedThrottle()
    admission = HeaderAdmission(throttle, 10, "text")
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    with header_admission(admission):
        await install_header_trace(request)
    trace = request.extensions["trace"]
    await trace("http11.send_request_headers.started", {"request": SimpleNamespace(method=b"CONNECT")})
    await trace("http11.send_request_headers.complete", {"return_value": None})
    assert not throttle.admissions
    assert not admission.reservations
    assert throttle.dispatch_lock.locked(), "Proxy CONNECT must retain the turn for model headers"
    admission.finish()
    assert not throttle.dispatch_lock.locked()


@pytest.mark.asyncio
async def test_real_success_headers_start_input_window_before_stream_completion():
    finish_stream = asyncio.Event()

    async def respond(request, writer):
        await send_sse(writer, finish_stream)

    class SplitObservedThrottle(ObservedThrottle):
        estimate = RequestThrottle.estimate

    async with LoopbackProvider(respond) as provider:
        client = sdk_client(provider)
        throttle = SplitObservedThrottle()
        job = asyncio.create_task(complete(client, throttle))
        try:
            await provider.wait_requests(1)
            # Reading headers happens after the peer records the HTTP request.
            for _ in range(100):
                if throttle.admissions and throttle.admissions[0].input_confirmed_at is not None:
                    break
                await asyncio.sleep(.005)
            record = throttle.admissions[0]
            assert record.input_confirmed_at is not None
            assert not job.done()
            snapshot = throttle.snapshot()
            assert snapshot["input_pending_tokens"] == 0
            assert snapshot["input_tokens_window"] == record.input_tokens > 0
            assert snapshot["output_reserved_tokens"] == 8192
            assert snapshot["token_estimated_total"] == record.initial_tokens
            finish_stream.set()
            await asyncio.wait_for(job, 5)
            assert throttle.snapshot()["token_estimated_total"] == 10
            assert throttle.snapshot()["inflight"] == 0
        finally:
            finish_stream.set()
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            await client.close()


@pytest.mark.asyncio
async def test_error_response_headers_do_not_acknowledge_input():
    throttle = ObservedThrottle()
    admission = HeaderAdmission(throttle, 10, "text")
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    with header_admission(admission):
        await install_header_trace(request)
    trace = request.extensions["trace"]
    await trace("http11.send_request_headers.started", {"request": SimpleNamespace(method=b"POST")})
    await trace("http11.send_request_headers.complete", {"return_value": None})
    await trace("http11.receive_response_headers.complete", {
        "return_value": (b"HTTP/1.1", 429, b"Limited", []),
    })
    record = admission.reservations[0]
    assert record.input_confirmed_at is None
    assert record.tokens == record.initial_tokens
    admission.finish(error=RuntimeError("rate limit"))
