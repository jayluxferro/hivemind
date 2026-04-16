"""Tests for the rate limit tracker."""

import asyncio
import time
import pytest

from hivemind.scheduler.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_initial_state():
    rl = RateLimiter()
    assert not rl.is_throttled
    assert rl.throttle_remaining_seconds == 0.0
    assert rl.get_window("default") is None


@pytest.mark.asyncio
async def test_parse_anthropic_headers():
    rl = RateLimiter()
    headers = {
        "anthropic-ratelimit-requests-remaining": "45",
        "anthropic-ratelimit-tokens-remaining": "80000",
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-tokens-limit": "100000",
    }
    await rl.update_from_headers(headers, provider="anthropic")

    window = rl.get_window("anthropic")
    assert window is not None
    assert window.remaining_requests == 45
    assert window.remaining_tokens == 80000
    assert window.limit_requests == 50
    assert window.limit_tokens == 100000


@pytest.mark.asyncio
async def test_parse_openai_headers():
    rl = RateLimiter()
    headers = {
        "x-ratelimit-remaining-requests": "20",
        "x-ratelimit-remaining-tokens": "50000",
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-limit-tokens": "100000",
    }
    await rl.update_from_headers(headers, provider="openai")

    window = rl.get_window("openai")
    assert window is not None
    assert window.remaining_requests == 20
    assert window.remaining_tokens == 50000


@pytest.mark.asyncio
async def test_retry_after_header():
    rl = RateLimiter()
    headers = {"retry-after": "3.0"}
    await rl.update_from_headers(headers)

    assert rl.is_throttled
    assert rl.throttle_remaining_seconds > 2.0


@pytest.mark.asyncio
async def test_proactive_throttle():
    rl = RateLimiter()
    headers = {
        "anthropic-ratelimit-requests-remaining": "1",
        "anthropic-ratelimit-requests-limit": "50",
    }
    await rl.update_from_headers(headers)
    # Should have triggered proactive throttle
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_wait_if_throttled():
    rl = RateLimiter()
    # Not throttled — should return immediately
    waited = await rl.wait_if_throttled()
    assert waited == 0.0


@pytest.mark.asyncio
async def test_stats():
    rl = RateLimiter()
    stats = rl.stats
    assert "is_throttled" in stats
    assert "providers" in stats
    assert isinstance(stats["providers"], dict)
