"""SSE envelope regression tests (SPEC §5).

All tests use mocked upstreams (httpx.MockTransport + AsyncByteStream) so no
network is required. They exercise the full ProxyServer -> Interceptor path.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from hivemind.proxy.server import ProxyServer
from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController
from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.storage.models import HiveMindConfig


def _stream_body(path: str = "/v1/messages") -> bytes:
    if path.startswith("/v1/chat/completions"):
        payload = {
            "model": "gpt-4o",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    else:
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    return json.dumps(payload).encode()


async def _consume_response(response) -> bytes:
    """Consume either a plain or streaming Starlette response."""
    from starlette.responses import StreamingResponse

    if isinstance(response, StreamingResponse):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)
    return response.body


async def _proxy(max_retries: int = 2) -> ProxyServer:
    config = HiveMindConfig(
        proxy_host="127.0.0.1",
        proxy_port=0,
        upstream_url="http://test-upstream",
        max_retries=max_retries,
        retry_base_delay=0.01,
        retry_max_delay=0.05,
        max_concurrency=5,
    )
    proxy = ProxyServer(
        config=config,
        admission=AdmissionController(5),
        rate_limiter=RateLimiter(),
        backpressure=BackpressureController(5),
        budget_manager=BudgetManager(),
        db=None,
    )
    await proxy.interceptor.start()
    return proxy


def _install_transport(proxy: ProxyServer, handler) -> None:
    proxy.interceptor._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        follow_redirects=True,
        verify=True,
        headers={"user-agent": ""},
    )


class _PartialThenReset(httpx.AsyncByteStream):
    """Emit one complete SSE event then raise a connection error."""

    async def __aiter__(self):
        yield b'event: message_start\ndata: {"type": "message_start"}\n\n'
        raise httpx.ConnectError("peer closed connection without sending complete message body")


class _ReadErrorEmpty(httpx.AsyncByteStream):
    """Emit one complete SSE event then raise httpx.ReadError with an empty str.

    This is what an abrupt upstream close looks like: httpx wraps anyio's
    EndOfStream in ReadError, and str(exc) is empty.
    """

    async def __aiter__(self):
        yield b'event: message_start\ndata: {"type": "message_start"}\n\n'
        raise httpx.ReadError("")


def _parse_sse(data: bytes) -> list[dict]:
    """Parse simple ``event/data`` SSE frames into a list of dicts."""
    events: list[dict] = []
    current: dict = {}
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("event: "):
            current["event"] = line[len("event: ") :]
        elif line.startswith("data: "):
            current.setdefault("data", []).append(line[len("data: ") :])
        elif line == "":
            if current:
                _finalize_event(current)
                events.append(current)
                current = {}
    if current:
        _finalize_event(current)
        events.append(current)
    return events


def _finalize_event(event: dict) -> None:
    try:
        event["json"] = json.loads("".join(event.get("data", [])))
    except json.JSONDecodeError:
        event["json"] = None


class _HappySSEStream(httpx.AsyncByteStream):
    """Emit a complete Anthropic-style SSE lifecycle."""

    async def __aiter__(self):
        yield b'event: message_start\ndata: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}\n\n'
        yield b'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}\n\n'
        yield b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello "}}\n\n'
        yield b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "world!"}}\n\n'
        yield b'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n'
        yield b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}\n\n'
        yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'


@pytest.mark.asyncio
async def test_streaming_upstream_401_returns_plain_json():
    """A. upstream 401 JSON for stream:true -> 401 JSON, never 200+SSE."""

    def handler(request):
        return httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}}).encode(),
        )

    proxy = await _proxy()
    try:
        _install_transport(proxy, handler)
        response = await proxy._handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body("/v1/messages"),
            agent_id=None,
        )

        assert response.status_code == 401
        assert "text/event-stream" not in response.headers.get("content-type", "")
        body = await _consume_response(response)
        data = json.loads(body)
        assert data["error"]["type"] == "authentication_error"
        assert response.headers["x-hivemind-retries"] == "0"
    finally:
        await proxy.interceptor.stop()


@pytest.mark.asyncio
async def test_streaming_mid_stream_reset_emits_terminal_frame():
    """B.1 partial SSE then upstream reset -> terminal error frame, not zero-frame EOF."""

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_PartialThenReset(),
        )

    proxy = await _proxy()
    try:
        _install_transport(proxy, handler)
        response = await proxy._handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body("/v1/messages"),
            agent_id=None,
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        body = await _consume_response(response)
        assert b"event: message_start" in body, "Should receive the one valid upstream frame"
        assert b"event: error" in body, "Should receive a terminal error frame"
        # The terminal error frame is the last event and is well-formed SSE.
        assert body.endswith(b"\n\n")
        assert body.count(b"event: error") == 1
        assert proxy.interceptor.admission.active == 0
    finally:
        await proxy.interceptor.stop()


@pytest.mark.asyncio
async def test_streaming_mid_stream_empty_str_error_is_typed(caplog):
    """B.2 abrupt upstream close (empty-str ReadError) -> terminal frame names the type.

    Regression: an abrupt close surfaces as httpx.ReadError wrapping
    anyio.EndOfStream, whose str() is EMPTY. The terminal SSE frame and the
    warning log must still name the exception type — otherwise the failure is
    invisible ("HiveMind: mid-stream failure: " with nothing after the colon,
    and an empty client-facing message).
    """

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_ReadErrorEmpty(),
        )

    proxy = await _proxy()
    try:
        _install_transport(proxy, handler)
        with caplog.at_level(logging.WARNING, logger="hivemind.proxy.interceptor"):
            response = await proxy._handle_streaming_request(
                "POST",
                "/v1/messages",
                {"content-type": "application/json", "accept": "text/event-stream"},
                _stream_body("/v1/messages"),
                agent_id=None,
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        body = await _consume_response(response)
        assert b"event: message_start" in body, "Should receive the one valid upstream frame"
        assert b"event: error" in body, "Should receive a terminal error frame"

        events = _parse_sse(body)
        assert events, "zero-frame EOF is a bug"
        assert events[-1]["event"] == "error"
        assert events[-1]["json"]["error"]["message"].startswith("ReadError")
        assert any("mid-stream failure" in r.getMessage() and "ReadError" in r.getMessage() for r in caplog.records)
        assert proxy.interceptor.admission.active == 0
    finally:
        await proxy.interceptor.stop()


@pytest.mark.asyncio
async def test_streaming_reset_before_headers_returns_plain_502():
    """B.3 upstream reset before headers -> plain 502/504 JSON, never empty 200 SSE."""

    def handler(request):
        raise httpx.ConnectError("connection reset by peer")

    proxy = await _proxy(max_retries=2)
    try:
        _install_transport(proxy, handler)
        response = await proxy._handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body("/v1/messages"),
            agent_id=None,
        )

        assert response.status_code == 502
        assert "text/event-stream" not in response.headers.get("content-type", "")
        assert response.headers.get("content-type") == "application/json"
        body = await _consume_response(response)
        assert b"error" in body
        assert response.headers["x-hivemind-retries"] == "2"
        assert proxy.interceptor.admission.active == 0
    finally:
        await proxy.interceptor.stop()


@pytest.mark.asyncio
async def test_streaming_happy_path():
    """C. Happy path: incremental SSE delivery and token counting preserved."""

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_HappySSEStream(),
        )

    proxy = await _proxy()
    try:
        _install_transport(proxy, handler)
        response = await proxy._handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body("/v1/messages"),
            agent_id=None,
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        body = await _consume_response(response)
        assert b"event: message_start" in body
        assert b"event: content_block_delta" in body
        assert b"event: message_stop" in body
        assert b"output_tokens" in body
        # Token counting should have recorded output tokens in the final message_delta.
        assert b'"output_tokens": 2' in body
        assert proxy.interceptor.admission.active == 0
    finally:
        await proxy.interceptor.stop()


@pytest.mark.asyncio
async def test_streaming_upstream_200_json_not_sse():
    """D. Upstream returns 200 JSON for stream:true -> must not be labeled SSE."""

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"object": "chat.completion", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 0}}).encode(),
        )

    proxy = await _proxy()
    try:
        _install_transport(proxy, handler)
        response = await proxy._handle_streaming_request(
            "POST",
            "/v1/chat/completions",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body("/v1/chat/completions"),
            agent_id=None,
        )

        assert response.status_code == 200
        assert response.headers.get("content-type") == "application/json"
        body = await _consume_response(response)
        data = json.loads(body)
        assert data["object"] == "chat.completion"
        assert "text/event-stream" not in response.headers.get("content-type", "")
    finally:
        await proxy.interceptor.stop()


@pytest.mark.asyncio
async def test_streaming_retry_before_headers_succeeds():
    """Streaming retry: first attempt fails pre-headers, retry succeeds."""

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_HappySSEStream(),
        )

    proxy = await _proxy(max_retries=2)
    try:
        _install_transport(proxy, handler)
        assert proxy.interceptor.admission.active == 0

        response = await proxy._handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body("/v1/messages"),
            agent_id=None,
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        body = await _consume_response(response)
        assert b"event: message_stop" in body
        assert len(calls) == 2, "First attempt should fail and be retried once"
        assert proxy.interceptor.retry_policy.stats["total_retries"] == 1
        # Admission slot acquired once and released exactly once.
        assert proxy.interceptor.admission.active == 0
    finally:
        await proxy.interceptor.stop()
