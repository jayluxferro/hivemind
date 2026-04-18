"""Tests for the rate limit tracker."""

import asyncio
import time
import pytest

from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.scheduler.providers import ANTHROPIC, OPENAI, OLLAMA


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


# --- Provider profile tests ---


@pytest.mark.asyncio
async def test_configure_from_profile():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)
    assert rl._rpm_limit == 50
    assert rl._tpm_limit == 80_000
    assert rl._provider_name == "Anthropic"

    stats = rl.stats
    assert stats["provider"] == "Anthropic"
    assert stats["rpm_limit"] == 50
    assert stats["rpm_current"] == 0


@pytest.mark.asyncio
async def test_rpm_throttle():
    """RPM counter should throttle when requests hit the limit."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)  # 50 RPM

    # Record 50 requests — should hit the limit
    for _ in range(50):
        rl.record_request()

    assert rl._rpm_wait_seconds() > 0
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_rpm_no_throttle_below_limit():
    rl = RateLimiter()
    rl.configure_from_profile(OPENAI)  # 60 RPM

    for _ in range(30):
        rl.record_request()

    assert rl._rpm_wait_seconds() == 0.0
    assert not rl.is_throttled


@pytest.mark.asyncio
async def test_tpm_throttle():
    """TPM counter should throttle when token usage hits the limit."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)  # 80K TPM

    rl.record_tokens(80_000)
    assert rl._tpm_wait_seconds() > 0
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_wait_records_request():
    """wait_if_throttled should record the request in the RPM window."""
    rl = RateLimiter()
    rl.configure_from_profile(OPENAI)

    await rl.wait_if_throttled()
    assert rl.stats["rpm_current"] == 1


@pytest.mark.asyncio
async def test_no_profile_no_rpm_throttle():
    """Without a profile, RPM/TPM enforcement is disabled."""
    rl = RateLimiter()
    for _ in range(1000):
        rl.record_request()
    assert rl._rpm_wait_seconds() == 0.0
    assert not rl.is_throttled


@pytest.mark.asyncio
async def test_stats_include_provider_fields():
    rl = RateLimiter()
    rl.configure_from_profile(OLLAMA)

    rl.record_request()
    rl.record_tokens(500)

    stats = rl.stats
    assert stats["provider"] == "Ollama (local)"
    assert stats["rpm_limit"] == 1000
    assert stats["rpm_current"] == 1
    assert stats["tpm_current"] == 500
