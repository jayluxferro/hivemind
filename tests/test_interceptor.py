"""Tests for the proxy interceptor — the core proxy logic."""

import asyncio
import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from hivemind.proxy.interceptor import Interceptor
from hivemind.proxy.latency_tracker import LatencyTracker
from hivemind.proxy.retry import RetryPolicy
from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController
from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.providers import ANTHROPIC, OPENAI, ProviderType
from hivemind.scheduler.rate_limiter import RateLimiter


@pytest.fixture
def components():
    return {
        "admission": AdmissionController(max_concurrency=5),
        "rate_limiter": RateLimiter(),
        "backpressure": BackpressureController(max_concurrency=5),
        "budget_manager": BudgetManager(),
        "latency_tracker": LatencyTracker(),
        "retry_policy": RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05),
    }


@pytest.fixture
def interceptor(components):
    return Interceptor(
        upstream_url="https://api.anthropic.com",
        **components,
    )


def _make_response(status_code=200, body=None, headers=None):
    """Create a mock httpx.Response."""
    if body is None:
        body = json.dumps(
            {
                "content": [{"type": "text", "text": "Hello"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        ).encode()
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = headers or {
        "content-type": "application/json",
        "anthropic-ratelimit-requests-remaining": "45",
    }
    return resp


@pytest.mark.asyncio
async def test_successful_request(interceptor):
    mock_response = _make_response()

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={"content-type": "application/json", "x-api-key": "test"},
        body=json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode(),
        agent_id="test-agent",
    )

    assert result.status_code == 200
    assert result.tokens_in == 100
    assert result.tokens_out == 50
    assert result.retries == 0


@pytest.mark.asyncio
async def test_admission_release_on_success(interceptor, components):
    mock_response = _make_response()
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    interceptor._client = mock_client

    admission = components["admission"]
    assert admission.active == 0

    await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    # Admission slot should be released
    assert admission.active == 0


@pytest.mark.asyncio
async def test_retry_on_429(interceptor):
    resp_429 = _make_response(status_code=429, body=b'{"error": "rate limited"}')
    resp_200 = _make_response(status_code=200)

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=[resp_429, resp_200])
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    assert result.status_code == 200
    assert result.retries == 1


@pytest.mark.asyncio
async def test_retry_exhausted(interceptor):
    resp_502 = _make_response(status_code=502, body=b'{"error": "bad gateway"}')

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp_502)
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    assert result.status_code == 502
    assert result.retries == 2  # max_retries=2


@pytest.mark.asyncio
async def test_connection_error_retry(interceptor):
    resp_200 = _make_response(status_code=200)

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=[ConnectionResetError("ECONNRESET"), resp_200])
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    assert result.status_code == 200
    assert result.retries == 1


@pytest.mark.asyncio
async def test_non_retryable_error(interceptor):
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=ValueError("invalid"))
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    assert result.status_code == 502
    assert result.retries == 0


@pytest.mark.asyncio
async def test_budget_tracking(interceptor, components):
    bm = components["budget_manager"]
    await bm.register_agent("agent-1", budget=100000)

    mock_response = _make_response()
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    interceptor._client = mock_client

    await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
        agent_id="agent-1",
    )

    ab = await bm.get_agent_budget("agent-1")
    assert ab.used == 150  # 100 in + 50 out


@pytest.mark.asyncio
async def test_rate_limit_headers_parsed(interceptor, components):
    rl = components["rate_limiter"]
    mock_response = _make_response(
        headers={
            "content-type": "application/json",
            "anthropic-ratelimit-requests-remaining": "10",
            "anthropic-ratelimit-requests-limit": "50",
        }
    )
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    interceptor._client = mock_client

    await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    window = rl.get_window("default")
    assert window is not None
    assert window.remaining_requests == 10


def _make_openai_response(status_code=200, body=None, headers=None):
    """Create a mock httpx.Response in OpenAI format."""
    if body is None:
        body = json.dumps(
            {
                "id": "chatcmpl-abc123",
                "object": "chat.completion",
                "model": "gpt-4o",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110},
            }
        ).encode()
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = headers or {
        "content-type": "application/json",
        "x-ratelimit-remaining-requests": "55",
        "x-ratelimit-remaining-tokens": "90000",
    }
    return resp


@pytest.fixture
def openai_interceptor(components):
    return Interceptor(
        upstream_url="https://api.openai.com",
        provider=OPENAI,
        **components,
    )


@pytest.mark.asyncio
async def test_openai_token_counting(openai_interceptor):
    """Verify token counts are correctly extracted from OpenAI responses."""
    mock_response = _make_openai_response()
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    openai_interceptor._client = mock_client

    result = await openai_interceptor.handle_request(
        method="POST",
        path="/v1/chat/completions",
        headers={"content-type": "application/json", "authorization": "Bearer test"},
        body=json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}).encode(),
        agent_id="openai-agent",
    )

    assert result.status_code == 200
    assert result.tokens_in == 80
    assert result.tokens_out == 30


@pytest.mark.asyncio
async def test_openai_rate_limit_headers(openai_interceptor, components):
    """Verify OpenAI rate limit headers are parsed correctly."""
    rl = components["rate_limiter"]
    mock_response = _make_openai_response(
        headers={
            "content-type": "application/json",
            "x-ratelimit-remaining-requests": "20",
            "x-ratelimit-remaining-tokens": "50000",
        }
    )
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    openai_interceptor._client = mock_client

    await openai_interceptor.handle_request(
        method="POST",
        path="/v1/chat/completions",
        headers={},
        body=b"{}",
    )

    window = rl.get_window("default")
    assert window is not None
    assert window.remaining_requests == 20


@pytest.mark.asyncio
async def test_openai_provider_no_529_retry(components):
    """OpenAI provider should NOT retry 529 (Anthropic-only status code)."""
    interceptor = Interceptor(
        upstream_url="https://api.openai.com",
        provider=OPENAI,
        **components,
    )

    resp_529 = _make_openai_response(status_code=529, body=b'{"error": "overloaded"}')

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=resp_529)
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/chat/completions",
        headers={},
        body=b"{}",
    )

    # 529 is NOT in OpenAI's retryable codes, so no retries
    assert result.status_code == 529
    assert result.retries == 0


@pytest.mark.asyncio
async def test_anthropic_provider_retries_529(components):
    """Anthropic provider SHOULD retry 529."""
    interceptor = Interceptor(
        upstream_url="https://api.anthropic.com",
        provider=ANTHROPIC,
        **components,
    )

    resp_529 = _make_response(status_code=529, body=b'{"error": "overloaded"}')
    resp_200 = _make_response(status_code=200)

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=[resp_529, resp_200])
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )

    assert result.status_code == 200
    assert result.retries == 1


def test_rebind_upstream(components):
    inc = Interceptor(upstream_url="https://api.anthropic.com", **components)
    assert "anthropic.com" in inc.upstream_url
    inc.rebind_upstream("https://api.openai.com/v1")
    assert "openai.com" in inc.upstream_url
    assert inc.provider is not None
    assert inc.provider.provider_type == ProviderType.OPENAI


@pytest.mark.asyncio
async def test_set_tls_verify_recreates_client(components):
    inc = Interceptor(
        upstream_url="https://api.anthropic.com",
        tls_verify=True,
        **components,
    )
    await inc.start()
    first_client = inc._client
    await inc.set_tls_verify(False)
    assert inc._client is not None
    assert inc._client is not first_client
    await inc.stop()


def test_forward_headers_strips_accept_encoding():
    """Regression: a proxy consumes upstream bytes before re-serving them, so
    it must not advertise encodings its own httpx cannot decode.  Forwarding
    a client's `br` when brotli isn't installed made DeepSeek/CloudFront send
    brotli bytes that reached the client raw with content-encoding stripped
    (undecodable binary labelled application/json)."""
    from hivemind.proxy.interceptor import _forward_headers

    out = _forward_headers(
        {
            "host": "example.com",
            "accept-encoding": "gzip, deflate, br, zstd",
            "Accept-Encoding": "gzip, deflate, br",
            "x-api-key": "k",
            "content-type": "application/json",
            "content-length": "123",
            "connection": "keep-alive",
        }
    )
    lowered = {k.lower() for k in out}
    assert "accept-encoding" not in lowered
    assert "host" not in lowered
    assert "content-length" not in lowered
    assert "connection" not in lowered
    assert out["x-api-key"] == "k"
    assert out["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_rate_limit_queue_full_returns_429_fast(components):
    """Regression: a deep rate-limiter queue once waited ~300s — every
    layer's read ceiling — surfacing as a bare gateway ReadTimeout
    (2026-09-01).  A projected wait beyond MAX_WAIT_S must fail fast with
    a 429 + retry-after instead of queueing."""
    from hivemind.scheduler.rate_limiter import MAX_WAIT_S

    limiter = components["rate_limiter"]
    limiter._wait_seconds = lambda agent_id: MAX_WAIT_S + 5.0

    interceptor = Interceptor(upstream_url="https://api.anthropic.com", **components)
    mock_client = AsyncMock()
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={"content-type": "application/json", "x-api-key": "test"},
        body=json.dumps({"messages": [{"role": "user", "content": "Hi"}]}).encode(),
        agent_id="test-agent",
    )

    assert result.status_code == 429
    assert mock_client.request.await_count == 0  # never reached upstream
    assert int(result.headers["retry-after"]) >= MAX_WAIT_S
    assert b"retry later" in result.body


# --- token-ledger hooks (SPEC-token-ledger §4) --------------------------------


class _RecordingLedger:
    """Stand-in ledger capturing every scheduled row (with a fail option)."""

    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict] = []
        self.fail = fail
        self.record_calls = 0

    async def record(self, row: dict) -> None:
        self.record_calls += 1
        if self.fail:
            raise RuntimeError("ledger down")
        self.rows.append(dict(row))


@pytest.fixture
def recording_ledger(monkeypatch):
    ledger = _RecordingLedger()
    monkeypatch.setattr("hivemind.proxy.interceptor.get_ledger", lambda: ledger)
    return ledger


async def _settle() -> None:
    """Let the fire-and-forget record() task run to completion."""
    for _ in range(10):
        await asyncio.sleep(0)


def _anthropic_interceptor(components, **kwargs):
    return Interceptor(
        upstream_url="https://api.anthropic.com",
        provider=ANTHROPIC,
        **components,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_telemetry_non_streaming_records_one_row(components, recording_ledger):
    """One buffered request -> exactly one row with every expected field."""
    body = json.dumps({"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}]}).encode()
    mock_response = _make_response(
        body=json.dumps(
            {
                "content": [{"type": "text", "text": "Hello"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 40,
                },
            }
        ).encode()
    )
    interceptor = _anthropic_interceptor(components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={"content-type": "application/json", "x-api-key": "test"},
        body=body,
        agent_id="agent-1",
        rate_key="bucket-7",
    )
    await _settle()

    assert result.status_code == 200
    assert recording_ledger.record_calls == 1
    assert len(recording_ledger.rows) == 1
    row = recording_ledger.rows[0]
    assert row["agent_hash"] == "bucket-7"  # rate_key takes precedence
    assert row["provider"] == "Anthropic"
    assert row["model"] == "claude-sonnet-4-20250514"
    assert row["tokens_in"] == 100
    assert row["tokens_out"] == 50
    assert row["cache_read"] == 40
    assert row["cache_write"] == 5
    assert row["reasoning"] is None
    assert isinstance(row["latency_ms"], float)
    assert row["status"] == 200


@pytest.mark.asyncio
async def test_telemetry_anonymous_when_unidentified(interceptor, recording_ledger):
    """No agent_id/rate_key and no model in the body -> 'anonymous'/'unknown'."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response())
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )
    await _settle()

    assert result.status_code == 200
    assert len(recording_ledger.rows) == 1
    row = recording_ledger.rows[0]
    assert row["agent_hash"] == "anonymous"
    assert row["provider"] == "unknown"  # this fixture builds no provider profile
    assert row["model"] == "unknown"
    assert row["status"] == 200


@pytest.mark.asyncio
async def test_telemetry_error_row_recorded_for_failed_request(interceptor, recording_ledger):
    """A non-retryable upstream error still yields a row with the real status."""
    mock_response = _make_response(status_code=502, body=b'{"error": "bad gateway"}')
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
        agent_id="agent-1",
    )
    await _settle()

    assert result.status_code == 502
    assert len(recording_ledger.rows) == 1
    assert recording_ledger.rows[0]["status"] == 502
    assert recording_ledger.rows[0]["agent_hash"] == "agent-1"


@pytest.mark.asyncio
async def test_telemetry_recorder_failure_never_breaks_request(components, monkeypatch):
    """A raising ledger must not disturb the request path (D4 fail-open)."""
    ledger = _RecordingLedger(fail=True)
    monkeypatch.setattr("hivemind.proxy.interceptor.get_ledger", lambda: ledger)

    interceptor = _anthropic_interceptor(components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response())
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={},
        body=b"{}",
    )
    await _settle()

    assert result.status_code == 200
    assert ledger.record_calls == 1  # hook fired; the raise stayed inside the task


class _CacheSSEStream(httpx.AsyncByteStream):
    """Anthropic lifecycle with cache usage carried in message_start."""

    async def __aiter__(self):
        yield (
            b'event: message_start\ndata: {"type": "message_start", "message": {"usage": '
            b'{"input_tokens": 12, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 40}}}\n\n'
        )
        yield b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}\n\n'
        yield b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}\n\n'
        yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'


def _stream_body() -> bytes:
    return json.dumps(
        {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()


@pytest.mark.asyncio
async def test_telemetry_streaming_full_drain_records_one_row(components, recording_ledger):
    """One committed SSE stream, fully consumed -> exactly one row."""

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_CacheSSEStream())

    interceptor = _anthropic_interceptor(components)
    interceptor._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        final = None
        async for _chunk, result in interceptor.handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body(),
            agent_id="agent-1",
            rate_key="bucket-9",
        ):
            if result is not None:
                final = result
        await _settle()

        assert final is not None and final.status_code == 200
        assert final.tokens_in == 12
        assert final.tokens_out == 7
        assert recording_ledger.record_calls == 1
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        assert row["agent_hash"] == "bucket-9"
        assert row["provider"] == "Anthropic"
        assert row["model"] == "claude-sonnet-4-20250514"
        assert row["tokens_in"] == 12
        assert row["tokens_out"] == 7
        assert row["cache_read"] == 40  # SSE per-key maxima, not lost to frame splitting
        assert row["cache_write"] == 5
        assert row["status"] == 200
        assert isinstance(row["latency_ms"], float)
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_telemetry_streaming_early_error_aclose_records_once(components, recording_ledger):
    """Server closes the generator right after the FIRST yield of an early
    error (401 flow): the outermost finally must still record exactly once."""

    def handler(request):
        return httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=b'{"type": "error", "error": {"type": "authentication_error", "message": "invalid key"}}',
        )

    interceptor = _anthropic_interceptor(components)
    interceptor._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        gen = interceptor.handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body(),
            agent_id="agent-1",
            rate_key="bucket-1",
        )
        iterator = gen.__aiter__()
        chunk, result = await iterator.__anext__()
        assert result.status_code == 401
        assert chunk
        await gen.aclose()  # GeneratorExit lands inside the streaming path
        await _settle()

        assert recording_ledger.record_calls == 1
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        assert row["agent_hash"] == "bucket-1"
        assert row["provider"] == "Anthropic"
        assert row["model"] == "claude-sonnet-4-20250514"
        assert row["status"] == 401
        assert row["tokens_in"] is None
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_telemetry_streaming_mid_stream_abort_records_once(components, recording_ledger):
    """Gate-2 abort after committed bytes still lands exactly one row."""

    class _AbruptSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_start\ndata: {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}\n\n'
            raise httpx.ReadError("")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_AbruptSSE())

    interceptor = _anthropic_interceptor(components)
    interceptor._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        final = None
        async for _chunk, result in interceptor.handle_streaming_request(
            "POST",
            "/v1/messages",
            {"content-type": "application/json", "accept": "text/event-stream"},
            _stream_body(),
            agent_id="agent-1",
        ):
            if result is not None:
                final = result
        await _settle()

        assert final is not None and final.status_code == 200  # committed status frozen
        assert final.error and "ReadError" in final.error
        assert recording_ledger.record_calls == 1
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        assert row["status"] == 200
        assert row["tokens_in"] == 10
        assert row["tokens_out"] is None
        assert row["agent_hash"] == "agent-1"
    finally:
        await interceptor.stop()
