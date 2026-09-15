"""Place paced admission at HTTP/1.1 request-header dispatch, after pool waits.

The trace hook is part of httpcore's extension API. HTTP/2 is deliberately off:
its shared connection write lock is acquired after this event. Network delivery
times remain outside this local dispatch guarantee.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from .request_throttle import DEFAULT_CONCURRENCY, RequestThrottle


@dataclass
class HeaderAdmission:
    throttle: RequestThrottle
    input_tokens: int
    kind: str
    reservations: list = field(default_factory=list)
    _dispatch_held: bool = False
    _dispatch_record: object | None = None

    async def prepare(self) -> None:
        # Request hooks run before connection-pool acquisition and TCP/TLS setup.
        # Otherwise a large backlog opens sockets that gateways can close while
        # they sit idle waiting for the first HTTP headers.
        if self._dispatch_held:
            return
        if self.reservations:
            self.throttle.finish(self.reservations[-1])
        await self.throttle.wait_for_dispatch()
        self._dispatch_held = True
        try:
            await self.throttle.wait_until_ready(
                self.throttle.estimate(self.input_tokens, self.kind),
            )
        except BaseException:
            self._release_dispatch()
            raise

    async def trace(self, name: str, info: dict) -> None:
        request = info.get("request")
        if getattr(request, "method", None) == b"CONNECT":
            return  # HTTPS proxy setup is not a model call.
        if name in {"http11.send_request_headers.complete", "http11.send_request_headers.failed"}:
            # CONNECT completion does not carry the request object. It must not
            # release the pre-connect turn before the actual model request.
            if self._dispatch_record is not None:
                self._release_dispatch()
            return
        if name != "http11.send_request_headers.started":
            return
        # The hook queues before connecting; only the actual header boundary
        # reserves tokens and counts a request. Recheck after network setup.
        if not self._dispatch_held:
            await self.prepare()
        try:
            reservation = await self.throttle.acquire(
                self.throttle.estimate(self.input_tokens, self.kind),
                input_proxy=self.input_tokens, kind=self.kind,
            )
            self.reservations.append(reservation)
            self._dispatch_record = reservation
        except BaseException:
            self._release_dispatch()
            raise
        # Keep only header writes serialized. Socket-write checkpoints cannot
        # release a backlog of old permits; response streams remain concurrent.

    def _release_dispatch(self) -> None:
        if self._dispatch_held:
            self._dispatch_held = False
            try:
                if self._dispatch_record is not None:
                    self.throttle.headers_sent(self._dispatch_record)
            finally:
                self._dispatch_record = None
                self.throttle.release_dispatch()

    def finish(self, usage=None, error=None) -> None:
        self._release_dispatch()
        if self.reservations:
            self.throttle.finish(self.reservations[-1], usage, error)


_admission: ContextVar[HeaderAdmission | None] = ContextVar("http_header_admission", default=None)


@contextmanager
def header_admission(admission: HeaderAdmission):
    token = _admission.set(admission)
    try:
        yield
    finally:
        _admission.reset(token)


async def install_header_trace(request: httpx.Request) -> None:
    admission = _admission.get()
    if admission is None:
        return
    await admission.prepare()
    previous = request.extensions.get("trace")
    if getattr(previous, "_auto_eval_admission", None) is admission:
        return  # HTTPX redirects copy extensions and run request hooks again.
    while hasattr(previous, "_auto_eval_admission"):
        previous = previous._auto_eval_previous_trace

    async def trace(name, info):
        if previous is not None:
            await previous(name, info)
        await admission.trace(name, info)

    trace._auto_eval_admission = admission
    trace._auto_eval_previous_trace = previous
    request.extensions["trace"] = trace


def paced_http_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    # Retain HTTPX's environment proxy/CA support and the SDK's redirect policy.
    # All clients share the same throttle, not one budget per connection pool.
    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=DEFAULT_CONCURRENCY, max_keepalive_connections=32),
        http2=False,
        follow_redirects=True,
        event_hooks={"request": [install_header_trace]},
    )
