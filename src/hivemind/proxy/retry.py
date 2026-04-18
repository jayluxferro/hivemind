"""Transparent retry logic for 429/502/ECONNRESET errors.

Agents don't know retries are happening — HiveMind handles it silently.
Uses exponential backoff with jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random

logger = logging.getLogger(__name__)

# Status codes that should trigger a retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


def is_retryable_error(error: Exception) -> bool:
    """Check if a connection error is retryable."""
    error_str = str(error).lower()
    retryable_patterns = [
        "econnreset",
        "connection reset",
        "broken pipe",
        "connection refused",
        "timeout",
        "connect timeout",
        "read timeout",
        "server disconnected",
        "remoteprotocolerror",
    ]
    return any(p in error_str for p in retryable_patterns)


def compute_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retry_after: float | None = None,
) -> float:
    """Compute retry delay with exponential backoff + jitter.

    If retry_after header was provided, use that as minimum.
    """
    # Exponential backoff: base * 2^attempt
    delay = base_delay * (2 ** attempt)
    # Add jitter (±25%)
    jitter = delay * 0.25 * (2 * random.random() - 1)
    delay = delay + jitter
    # Cap at max
    delay = min(delay, max_delay)
    # Respect retry-after header
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


class RetryPolicy:
    """Configurable retry policy for API requests."""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._total_retries = 0
        self._total_retry_time = 0.0

    def should_retry(self, attempt: int, status_code: int | None = None, error: Exception | None = None) -> bool:
        if attempt >= self.max_retries:
            return False
        if status_code is not None and is_retryable_status(status_code):
            return True
        if error is not None and is_retryable_error(error):
            return True
        return False

    async def wait(self, attempt: int, retry_after: float | None = None) -> float:
        """Wait before retrying. Returns the delay in seconds."""
        delay = compute_delay(
            attempt,
            base_delay=self.base_delay,
            max_delay=self.max_delay,
            retry_after=retry_after,
        )
        logger.info("Retry: attempt %d, waiting %.1fs", attempt + 1, delay)
        await asyncio.sleep(delay)
        self._total_retries += 1
        self._total_retry_time += delay
        return delay

    @property
    def stats(self) -> dict:
        return {
            "max_retries": self.max_retries,
            "total_retries": self._total_retries,
            "total_retry_time_seconds": round(self._total_retry_time, 2),
        }
