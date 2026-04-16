"""Tests for retry logic."""

import pytest

from hivemind.proxy.retry import (
    RetryPolicy,
    compute_delay,
    is_retryable_error,
    is_retryable_status,
)


def test_retryable_status_codes():
    assert is_retryable_status(429) is True
    assert is_retryable_status(502) is True
    assert is_retryable_status(503) is True
    assert is_retryable_status(500) is True
    assert is_retryable_status(529) is True
    assert is_retryable_status(200) is False
    assert is_retryable_status(400) is False
    assert is_retryable_status(404) is False


def test_retryable_errors():
    assert is_retryable_error(ConnectionResetError("ECONNRESET")) is True
    assert is_retryable_error(Exception("connection reset by peer")) is True
    assert is_retryable_error(TimeoutError("read timeout")) is True
    assert is_retryable_error(ValueError("invalid json")) is False


def test_compute_delay():
    # First attempt
    d0 = compute_delay(0, base_delay=1.0, max_delay=30.0)
    assert 0.5 <= d0 <= 1.5  # 1.0 ± 25% jitter

    # Second attempt (2x)
    d1 = compute_delay(1, base_delay=1.0, max_delay=30.0)
    assert 1.0 <= d1 <= 3.0

    # Should cap at max
    d10 = compute_delay(10, base_delay=1.0, max_delay=30.0)
    assert d10 <= 30.0


def test_compute_delay_with_retry_after():
    d = compute_delay(0, base_delay=1.0, max_delay=30.0, retry_after=10.0)
    assert d >= 10.0


def test_retry_policy_should_retry():
    rp = RetryPolicy(max_retries=3)

    assert rp.should_retry(0, status_code=429) is True
    assert rp.should_retry(2, status_code=502) is True
    assert rp.should_retry(3, status_code=429) is False  # Exceeded max
    assert rp.should_retry(0, status_code=400) is False  # Not retryable


def test_retry_policy_should_retry_error():
    rp = RetryPolicy(max_retries=3)
    assert rp.should_retry(0, error=ConnectionResetError("connection reset")) is True
    assert rp.should_retry(0, error=ValueError("invalid")) is False


@pytest.mark.asyncio
async def test_retry_policy_wait():
    rp = RetryPolicy(max_retries=3, base_delay=0.01, max_delay=0.1)
    delay = await rp.wait(0)
    assert delay > 0
    assert rp.stats["total_retries"] == 1
