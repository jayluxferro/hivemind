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
        body = json.dumps({
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }).encode()
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
    mock_client.request = AsyncMock(
        side_effect=[ConnectionResetError("ECONNRESET"), resp_200]
    )
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
