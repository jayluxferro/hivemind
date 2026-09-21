"""Tests for the proxy interceptor — the core proxy logic."""

import asyncio
import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock

from hivemind.proxy.interceptor import Interceptor, _observed_output_tokens
from hivemind.proxy.latency_tracker import LatencyTracker
from hivemind.proxy.retry import RetryPolicy
from hivemind.proxy.token_counter import count_request_tokens
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
    # The row's numbers ARE the provider-reported counts, carried on the
    # result as explicit observations (not the padded operational totals).
    assert result.observed_tokens_in == 100
    assert result.observed_tokens_out == 50
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


@pytest.mark.asyncio
async def test_telemetry_estimates_stay_out_of_the_row_but_reach_the_limiter_and_budget(
    components, recording_ledger, monkeypatch
):
    """A response with NO usage block: the ledger row records both token
    columns as NULL (rows are observations — D2), while the rate limiter and
    the budget still count the estimate-padded operational totals, because
    unreported traffic must count against the window (and against the
    wallet).  The output estimate is the sneaky one: count_response_tokens
    fabricates it from the response text, so the row would otherwise carry a
    guess indistinguishable from a provider-reported count."""
    recorded: list[int] = []
    monkeypatch.setattr(RateLimiter, "record_tokens", lambda self, count, agent_id=None: recorded.append(count))
    budget_calls: list[tuple] = []

    async def _record_usage(self, agent_id, tokens_in, tokens_out):
        budget_calls.append((agent_id, tokens_in, tokens_out))

    monkeypatch.setattr(BudgetManager, "record_usage", _record_usage)

    body = json.dumps({"model": "claude-sonnet-4-20250514", "messages": [{"role": "user", "content": "Hi"}]}).encode()
    resp_body = json.dumps({"content": [{"type": "text", "text": "Hello"}]}).encode()  # no "usage" block
    interceptor = _anthropic_interceptor(components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response(body=resp_body))
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={"content-type": "application/json", "x-api-key": "test"},
        body=body,
        agent_id="agent-1",
    )
    await _settle()

    # The operational counters on the result are estimate-padded...
    assert result.tokens_in == count_request_tokens(body)
    assert result.observed_tokens_in is None and result.observed_tokens_out is None
    # ...and those padded totals are what reached the rate limiter...
    assert recorded == [result.tokens_in + result.tokens_out]
    # ...and the budget.
    assert budget_calls == [("agent-1", result.tokens_in, result.tokens_out)]
    # But the row records the observations: nothing reported -> NULL, not the guess.
    assert len(recording_ledger.rows) == 1
    assert recording_ledger.rows[0]["tokens_in"] is None
    assert recording_ledger.rows[0]["tokens_out"] is None


def test_observed_output_tokens_reads_the_usage_block_only():
    """The ledger's observance check for buffered ``tokens_out``: only what
    the usage block says counts (both provider formats).  Everything else —
    no usage, unparseable body, a reported zero — is None, the NULL sentinel
    that keeps guesses out of the same column as real counts."""
    anthropic = json.dumps({"content": [], "usage": {"input_tokens": 3, "output_tokens": 9}}).encode()
    openai = json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 9}}).encode()
    assert _observed_output_tokens(anthropic) == 9
    assert _observed_output_tokens(openai) == 9
    assert _observed_output_tokens(b'{"content": [{"type": "text", "text": "Hello"}]}') is None
    assert _observed_output_tokens(b"not json at all") is None
    assert _observed_output_tokens(json.dumps({"usage": {"output_tokens": 0}}).encode()) is None


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
        assert final.stream_aborted is False  # clean stream: the row's 200 is real
        assert final.tokens_in == 12
        assert final.tokens_out == 7
        # Parity pin: the streaming path never estimates, so the observed
        # fields mirror the totals — the row rule (observed only) holds here
        # without the estimate-padding the buffered path needs.
        assert final.observed_tokens_in == 12
        assert final.observed_tokens_out == 7
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
    """Gate-2 abort after committed bytes still lands exactly one row —
    with the ledger status rewritten to 502 (the wire status stays 200)."""

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
        assert final.stream_aborted is True
        assert recording_ledger.record_calls == 1
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        # The LEDGER row must say 502: the wire status was frozen at 200
        # before the upstream died, and error_rate was blind to every abort
        # while rows parroted the frozen status.
        assert row["status"] == 502
        # Real (observed) token counts still travel with the abort.
        assert row["tokens_in"] == 10
        assert row["tokens_out"] is None
        assert row["agent_hash"] == "agent-1"
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_telemetry_streaming_without_usage_records_null_tokens(components, recording_ledger):
    """A committed stream that never reports usage: the row's tokens_in is
    NULL — the request estimate that pads result.tokens_in (and feeds the
    rate limiter) must not masquerade as an observation in the ledger."""

    class _NoUsageSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_start\ndata: {"type": "message_start", "message": {"role": "assistant"}}\n\n'
            yield b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}\n\n'
            yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_NoUsageSSE())

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

        assert final is not None and final.status_code == 200
        # Operational counter is estimate-padded, exactly as before...
        assert final.tokens_in == count_request_tokens(_stream_body())
        assert final.observed_tokens_in is None
        # ...but the row records the honest NULL on both counters.
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        assert row["tokens_in"] is None
        assert row["tokens_out"] is None
        assert row["status"] == 200
    finally:
        await interceptor.stop()


# --- fresh-only ingest normalization (provider usage shapes) -----------------
#
# The hole this closes: the ledger prices ``tokens_in`` as the FRESH input
# portion, which is what Anthropic-shape providers report — but OpenAI-shape
# providers report prompt_tokens INCLUDING cached_tokens while hivemind ALSO
# records their cached_tokens in ``cache_read``.  Left alone, every cached
# token is priced twice (price_in + price_cache_read): the attacker's probe
# showed 100k fresh + 400k cached at the seed prices must cost $0.0550, while
# the raw total-shape row read $0.1630 (+196%).


def _openai_interceptor(components, **kwargs):
    return Interceptor(
        upstream_url="https://api.openai.com",
        provider=OPENAI,
        **components,
        **kwargs,
    )


def _openai_usage_response(prompt: int, cached: int, completion: int = 10) -> bytes:
    """OpenAI-shape chat completion: prompt_tokens INCLUDES cached_tokens."""
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        }
    ).encode()


@pytest.mark.asyncio
async def test_telemetry_openai_total_shape_normalizes_cached_out_of_tokens_in(components, recording_ledger):
    """Buffered fake provider, OpenAI usage shape: 500k prompt_tokens of
    which 400k cached must record tokens_in == 100k — the fresh portion —
    with cache_read carrying the 400k.  The operational counters keep the
    provider's raw totals (normalization is ledger-row only)."""
    body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}).encode()
    interceptor = _openai_interceptor(components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value=_make_response(body=_openai_usage_response(prompt=500_000, cached=400_000))
    )
    interceptor._client = mock_client

    result = await interceptor.handle_request(
        method="POST",
        path="/v1/chat/completions",
        headers={"content-type": "application/json", "authorization": "Bearer test"},
        body=body,
        agent_id="agent-1",
    )
    await _settle()

    assert len(recording_ledger.rows) == 1
    row = recording_ledger.rows[0]
    assert row["provider"] == "OpenAI"
    assert row["model"] == "gpt-4o"
    assert row["tokens_in"] == 100_000  # 500k reported total - 400k cached
    assert row["cache_read"] == 400_000
    assert row["tokens_out"] == 10

    # The result itself still carries the RAW observation: only the row is
    # normalized (rate limiter / budgets / headers keep provider totals).
    assert result.observed_tokens_in == 500_000
    assert result.tokens_in == 500_000


@pytest.mark.asyncio
async def test_telemetry_streaming_openai_total_shape_normalizes_cached_out_of_tokens_in(components, recording_ledger):
    """Committed SSE stream, OpenAI shape (usage rides the final chunk):
    the accumulated prompt total is normalized against the per-key maxima
    of cached_tokens exactly as the buffered path normalizes its snapshot."""

    class _OpenAIUsageSSE(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "Hi"}}]}\n\n'
            yield b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
            yield (
                b'data: {"choices": [], "usage": {"prompt_tokens": 500000, "completion_tokens": 10, '
                b'"prompt_tokens_details": {"cached_tokens": 400000}}}\n\n'
            )
            yield b"data: [DONE]\n\n"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_OpenAIUsageSSE())

    interceptor = _openai_interceptor(components)
    interceptor._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        final = None
        async for _chunk, result in interceptor.handle_streaming_request(
            "POST",
            "/v1/chat/completions",
            {"content-type": "application/json", "accept": "text/event-stream"},
            json.dumps({"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode(),
            agent_id="agent-1",
        ):
            if result is not None:
                final = result
        await _settle()

        assert final is not None and final.status_code == 200
        # Operational counter: the provider's raw 500k (rate limiter sees it).
        assert final.tokens_in == 500_000
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        assert row["tokens_in"] == 100_000  # fresh portion after normalization
        assert row["cache_read"] == 400_000  # SSE per-key maxima
        assert row["tokens_out"] == 10
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_telemetry_fresh_shape_rows_pass_through_untouched(components, recording_ledger):
    """Anthropic/DeepSeek shape parity: input_tokens is ALREADY fresh-only
    (cache reads reported separately), so the ANTHROPIC profile must NOT be
    reduced — same real traffic as the OpenAI-shape tests records the
    identical fresh-only row (100k / 400k) with no normalization applied."""
    body = json.dumps({"model": "deepseek-chat", "messages": [{"role": "user", "content": "Hi"}]}).encode()
    resp_body = json.dumps(
        {
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {
                "input_tokens": 100_000,
                "output_tokens": 10,
                "cache_read_input_tokens": 400_000,
            },
        }
    ).encode()
    interceptor = _anthropic_interceptor(components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=_make_response(body=resp_body))
    interceptor._client = mock_client

    await interceptor.handle_request(
        method="POST",
        path="/v1/messages",
        headers={"content-type": "application/json", "x-api-key": "test"},
        body=body,
        agent_id="agent-1",
    )
    await _settle()

    assert len(recording_ledger.rows) == 1
    row = recording_ledger.rows[0]
    assert row["provider"] == "Anthropic"
    assert row["tokens_in"] == 100_000  # already fresh: untouched
    assert row["cache_read"] == 400_000


@pytest.mark.asyncio
async def test_telemetry_normalization_clamps_impossible_cache_counts(components, recording_ledger, caplog):
    """A provider glitch that reports more cached tokens than total input
    must never write a negative tokens_in — the usage_cost view would price
    it as negative money.  Clamped to 0 with a warning; all-cached (fresh
    exactly 0) is a real observation and also records 0, not NULL."""
    import logging

    body = json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}).encode()
    interceptor = _openai_interceptor(components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value=_make_response(body=_openai_usage_response(prompt=100_000, cached=400_000))
    )
    interceptor._client = mock_client

    with caplog.at_level(logging.WARNING, logger="hivemind.proxy.interceptor"):
        await interceptor.handle_request(
            method="POST",
            path="/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=body,
            agent_id="agent-1",
        )
    await _settle()

    assert len(recording_ledger.rows) == 1
    row = recording_ledger.rows[0]
    assert row["tokens_in"] == 0  # clamped: 100k total - 400k cached < 0
    assert row["cache_read"] == 400_000
    assert any("usage glitch" in r.message and r.levelno == logging.WARNING for r in caplog.records)

    # Boundary case at exactly zero: 400k of 400k cached is all-cached, not
    # a glitch — records 0 without the warning.
    mock_client.request = AsyncMock(
        return_value=_make_response(body=_openai_usage_response(prompt=400_000, cached=400_000))
    )
    await interceptor.handle_request(
        method="POST",
        path="/v1/chat/completions",
        headers={"content-type": "application/json"},
        body=body,
        agent_id="agent-1",
    )
    await _settle()

    assert len(recording_ledger.rows) == 2
    assert recording_ledger.rows[1]["tokens_in"] == 0


class _ModernCumulativeSSE(httpx.AsyncByteStream):
    """MODERN Anthropic lifecycle: BOTH usage snapshots are cumulative.

    message_start and message_delta each carry the full usage block
    (input_tokens + cache_read_input_tokens + output_tokens) — unlike the
    legacy shape (_CacheSSEStream above) where input rode only message_start
    and output only message_delta."""

    async def __aiter__(self):
        yield (
            b'event: message_start\ndata: {"type": "message_start", "message": {"model": "deepseek-chat", "usage": '
            b'{"input_tokens": 100, "cache_read_input_tokens": 400000, "output_tokens": 1}}}\n\n'
        )
        yield b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}\n\n'
        yield (
            b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, '
            b'"usage": {"input_tokens": 100, "cache_read_input_tokens": 400000, "output_tokens": 42}}\n\n'
        )
        yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'


@pytest.mark.asyncio
async def test_streaming_cumulative_usage_snapshots_merge_by_maxima(components, recording_ledger, caplog):
    """An ORDINARY modern cached stream reports its usage block twice
    (cumulative snapshots).  Summing the snapshots counted one prompt twice
    (100+100 for a 100-token input) and inflated the ledger row verbatim on
    fresh-shape upstreams; on total-shape upstreams the inflated total plus
    the per-key cache maxima (400k) tripped the fresh-only clamp on every
    cached request.  The merge must take per-key MAXIMA — the final snapshot
    IS the billed total — matching the rule the cache fields already used."""
    import logging

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_ModernCumulativeSSE())

    interceptor = _anthropic_interceptor(components)
    interceptor._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        final = None
        with caplog.at_level(logging.DEBUG, logger="hivemind.proxy.interceptor"):
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

        assert final is not None and final.status_code == 200
        # The provider billed 100 input / 42 output — NOT the summed 200/43.
        assert final.tokens_in == 100
        assert final.tokens_out == 42
        assert final.observed_tokens_in == 100
        assert final.observed_tokens_out == 42
        assert len(recording_ledger.rows) == 1
        row = recording_ledger.rows[0]
        assert row["tokens_in"] == 100  # fresh shape: verbatim, no clamp ran
        assert row["tokens_out"] == 42
        assert row["cache_read"] == 400_000  # per-key maxima across snapshots
        # And nothing about this ordinary stream is a "usage glitch".
        assert not [r for r in caplog.records if "usage glitch" in r.getMessage()]
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_usage_glitch_warning_fires_once_per_provider_model(components, recording_ledger, caplog, monkeypatch):
    """A shape-mismatched upstream can trip the clamp on every cached
    request (per-key cache maxima vs a smaller input snapshot), so the
    WARNING is rate-limited to once per (provider, model) per process;
    repeats drop to DEBUG — still visible, never a flood."""
    import logging

    from hivemind.proxy import interceptor as interceptor_module
    from hivemind.proxy.streaming import StreamingResult

    monkeypatch.setattr(interceptor_module, "_USAGE_GLITCH_WARNED", set())
    interceptor = _openai_interceptor(components)

    def _glitch_result() -> StreamingResult:
        result = StreamingResult(
            status_code=200,
            tokens_in=100_000,
            tokens_out=10,
            observed_tokens_in=100_000,
            observed_tokens_out=10,
        )
        result._cache_read_tokens = 400_000
        result._cache_write_tokens = None
        return result

    body = json.dumps({"model": "glm-clamp-volume", "messages": [{"role": "user", "content": "Hi"}]}).encode()
    with caplog.at_level(logging.DEBUG, logger="hivemind.proxy.interceptor"):
        row1 = interceptor._usage_row(_glitch_result(), body=body, agent_id="a", rate_key="a")
        row2 = interceptor._usage_row(_glitch_result(), body=body, agent_id="a", rate_key="a")
        # A different model under the same provider is a separate key: warns again.
        other_body = json.dumps({"model": "glm-clamp-other", "messages": []}).encode()
        interceptor._usage_row(_glitch_result(), body=other_body, agent_id="a", rate_key="a")
    await _settle()

    assert row1["tokens_in"] == 0 and row2["tokens_in"] == 0  # clamp still bites every time
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "usage glitch" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "usage glitch" in r.getMessage()]
    assert len(warnings) == 2  # once per (OpenAI, glm-clamp-volume) and (OpenAI, glm-clamp-other)
    assert len(debugs) == 1  # the repeat dropped to DEBUG


@pytest.mark.asyncio
async def test_shape_mismatched_upstream_warns_once_across_requests(components, recording_ledger, caplog, monkeypatch):
    """The hostile probe's P2 flood, end to end: a modern cumulative cached
    stream against a True-flagged profile.  With the maxima merge the input
    snapshot is the true 100 (not the summed 200), the mismatch still clamps
    (100 fresh-shape input vs 400k cache is a genuine shape error, and the
    clamp is the last line of the ledger's defense), but the WARNING fires
    on the FIRST request only — request two logs DEBUG, the log keeps one
    line instead of one per request.  The operator fix for the mismatch
    itself is --input-excludes-cached (Finding 3's escape hatch)."""
    import logging

    from hivemind.proxy import interceptor as interceptor_module

    monkeypatch.setattr(interceptor_module, "_USAGE_GLITCH_WARNED", set())

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_ModernCumulativeSSE())

    interceptor = _openai_interceptor(components)
    interceptor._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.DEBUG, logger="hivemind.proxy.interceptor"):
            for _request in range(2):
                async for _chunk, _result in interceptor.handle_streaming_request(
                    "POST",
                    "/v1/messages",
                    {"content-type": "application/json", "accept": "text/event-stream"},
                    _stream_body(),
                    agent_id="agent-1",
                ):
                    pass
        await _settle()

        assert len(recording_ledger.rows) == 2
        assert all(row["tokens_in"] == 0 for row in recording_ledger.rows)  # clamped both times
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "usage glitch" in r.getMessage()]
        debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "usage glitch" in r.getMessage()]
        assert len(warnings) == 1  # one line, not a flood
        assert len(debugs) == 1  # the second request still left a DEBUG trace
    finally:
        await interceptor.stop()


@pytest.mark.asyncio
async def test_telemetry_without_a_profile_cannot_normalize(components, recording_ledger):
    """No profile, no shape knowledge: the row passes the raw observation
    through verbatim rather than guessing whether input includes cache.
    (Production interceptors always carry a profile — detect_provider never
    returns None — so this only guards direct constructions.)"""
    interceptor = Interceptor(upstream_url="https://example.internal", **components)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        return_value=_make_response(body=_openai_usage_response(prompt=500_000, cached=400_000))
    )
    interceptor._client = mock_client

    await interceptor.handle_request(
        method="POST",
        path="/v1/chat/completions",
        headers={"content-type": "application/json"},
        body=b"{}",
        agent_id="agent-1",
    )
    await _settle()

    assert len(recording_ledger.rows) == 1
    assert recording_ledger.rows[0]["provider"] == "unknown"
    assert recording_ledger.rows[0]["tokens_in"] == 500_000  # verbatim


# --- conversation hash (ledger attribution) --------------------------------


def test_conversation_hash_prefers_the_first_session_header():
    from hivemind.proxy.interceptor import _conversation_hash

    headers = {
        "x-claude-code-session-id": "sess-aaa",
        "x-cursor-session-id": "sess-bbb",
    }
    assert _conversation_hash(headers) == _conversation_hash({"x-claude-code-session-id": "sess-aaa"})
    # Deterministic and hashed, never the raw value.
    import hashlib

    expected = hashlib.sha256(b"sess-aaa").hexdigest()[:16]
    assert _conversation_hash(headers) == expected
    assert "sess-aaa" not in expected


def test_conversation_hash_falls_back_across_header_kinds():
    from hivemind.proxy.interceptor import _conversation_hash

    assert _conversation_hash({"x-cursor-session-id": "c1"}) is not None
    assert _conversation_hash({"x-codex-session-id": "c2"}) is not None
    assert _conversation_hash({"authorization": "bearer x"}) is None
    assert _conversation_hash({}) is None
    assert _conversation_hash({"x-claude-code-session-id": "  "}) is None


async def test_usage_row_carries_conversation_hash(components):
    """The ledger row must carry the hashed session header so analysis can
    separate new-session starts from mid-session cache misses."""
    interceptor = Interceptor(upstream_url="https://api.anthropic.com", **components)
    await interceptor.start()
    try:
        row = interceptor._usage_row(
            _result(),
            body=b'{"model": "m"}',
            agent_id="a",
            rate_key=None,
            headers={"x-claude-code-session-id": "sess-1"},
        )
        from hivemind.proxy.interceptor import _conversation_hash

        assert row["conversation_hash"] == _conversation_hash({"x-claude-code-session-id": "sess-1"})
        assert interceptor._usage_row(_result(), body=b"{}", agent_id="a", rate_key=None)["conversation_hash"] is None
    finally:
        await interceptor.stop()


def _result():
    from hivemind.proxy.interceptor import InterceptResult

    return InterceptResult(status_code=200, headers={}, body=b"{}")


def test_rebind_without_profile_warns_on_shape_flip(components, caplog):
    """Round-eight visibility finding: a bare rebind_upstream re-detects
    from the URL and can silently change ledger pricing shape (dropping an
    operator override).  The flip must WARN, naming both shapes."""
    import logging

    interceptor = Interceptor(upstream_url="https://api.anthropic.com", **components)
    assert interceptor.provider.input_includes_cached is False

    with caplog.at_level(logging.WARNING, logger="hivemind.proxy.interceptor"):
        interceptor.rebind_upstream("https://api.openai.com/v1")  # no profile

    assert interceptor.provider.input_includes_cached is True
    assert any("input_includes_cached False -> True" in r.message for r in caplog.records)


def test_rebind_with_profile_never_warns(components, caplog):
    import logging

    from hivemind.scheduler.providers import OPENAI, resolve_provider_profile

    interceptor = Interceptor(upstream_url="https://api.anthropic.com", **components)
    with caplog.at_level(logging.WARNING, logger="hivemind.proxy.interceptor"):
        # The production caller's shape: resolved profile passed explicitly.
        resolved = resolve_provider_profile("https://api.openai.com/v1", None)
        assert resolved is OPENAI
        interceptor.rebind_upstream("https://api.openai.com/v1", resolved)
    assert not [r for r in caplog.records if "input_includes_cached" in r.message]
    assert interceptor.provider is OPENAI
