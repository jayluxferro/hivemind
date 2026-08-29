"""Tests for the proxy interceptor — the core proxy logic."""

import json
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
