"""Rate limit tracker — parses provider headers and proactively throttles.

Reads x-ratelimit-* headers from API responses and pauses requests
before hitting limits, not after.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RateLimitWindow:
    """Snapshot of a rate limit window from response headers."""

    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    limit_requests: int | None = None
    limit_tokens: int | None = None
    reset_requests_at: float | None = None  # unix timestamp
    reset_tokens_at: float | None = None
    updated_at: float = field(default_factory=time.time)


class RateLimiter:
    """Tracks rate limit state from API response headers and gates requests."""

    def __init__(self) -> None:
        self._windows: dict[str, RateLimitWindow] = {}
        self._lock = asyncio.Lock()
        # Threshold: if remaining requests < this fraction of limit, start throttling
        self._throttle_threshold = 0.1
        self._pause_until: float = 0.0

    @property
    def is_throttled(self) -> bool:
        return time.time() < self._pause_until

    @property
    def throttle_remaining_seconds(self) -> float:
        return max(0.0, self._pause_until - time.time())

    def get_window(self, provider: str = "default") -> RateLimitWindow | None:
        return self._windows.get(provider)

    async def update_from_headers(self, headers: dict[str, str], provider: str = "default") -> None:
        """Parse rate limit headers from an API response.

        Supports both Anthropic and OpenAI header formats:
          anthropic-ratelimit-requests-remaining
          anthropic-ratelimit-tokens-remaining
          x-ratelimit-remaining-requests
          x-ratelimit-remaining-tokens
          retry-after
        """
        async with self._lock:
            window = self._windows.get(provider, RateLimitWindow())

            # Anthropic headers
            if "anthropic-ratelimit-requests-remaining" in headers:
                window.remaining_requests = int(headers["anthropic-ratelimit-requests-remaining"])
            if "anthropic-ratelimit-tokens-remaining" in headers:
                window.remaining_tokens = int(headers["anthropic-ratelimit-tokens-remaining"])
            if "anthropic-ratelimit-requests-limit" in headers:
                window.limit_requests = int(headers["anthropic-ratelimit-requests-limit"])
            if "anthropic-ratelimit-tokens-limit" in headers:
                window.limit_tokens = int(headers["anthropic-ratelimit-tokens-limit"])
            if "anthropic-ratelimit-requests-reset" in headers:
                window.reset_requests_at = self._parse_reset(headers["anthropic-ratelimit-requests-reset"])
            if "anthropic-ratelimit-tokens-reset" in headers:
                window.reset_tokens_at = self._parse_reset(headers["anthropic-ratelimit-tokens-reset"])

            # OpenAI / generic headers
            if "x-ratelimit-remaining-requests" in headers:
                window.remaining_requests = int(headers["x-ratelimit-remaining-requests"])
            if "x-ratelimit-remaining-tokens" in headers:
                window.remaining_tokens = int(headers["x-ratelimit-remaining-tokens"])
            if "x-ratelimit-limit-requests" in headers:
                window.limit_requests = int(headers["x-ratelimit-limit-requests"])
            if "x-ratelimit-limit-tokens" in headers:
                window.limit_tokens = int(headers["x-ratelimit-limit-tokens"])

            # retry-after (seconds)
            if "retry-after" in headers:
                try:
                    delay = float(headers["retry-after"])
                    self._pause_until = max(self._pause_until, time.time() + delay)
                    logger.info("Rate limiter: retry-after %.1fs, pausing until %.0f", delay, self._pause_until)
                except ValueError:
                    pass

            window.updated_at = time.time()
            self._windows[provider] = window

            # Proactive throttle: if we're close to the limit, pause
            self._check_proactive_throttle(window)

    def _check_proactive_throttle(self, window: RateLimitWindow) -> None:
        """If remaining requests/tokens are low, proactively pause."""
        if window.remaining_requests is not None and window.limit_requests:
            ratio = window.remaining_requests / window.limit_requests
            if ratio < self._throttle_threshold and window.remaining_requests <= 2:
                # Calculate pause duration from reset time
                if window.reset_requests_at:
                    pause_until = window.reset_requests_at
                else:
                    pause_until = time.time() + 5.0  # default 5s pause
                self._pause_until = max(self._pause_until, pause_until)
                logger.warning(
                    "Rate limiter: proactive throttle — %d/%d requests remaining, pausing %.1fs",
                    window.remaining_requests,
                    window.limit_requests,
                    self._pause_until - time.time(),
                )

    async def wait_if_throttled(self) -> float:
        """Block until we're allowed to make a request. Returns seconds waited."""
        wait_time = self.throttle_remaining_seconds
        if wait_time > 0:
            logger.info("Rate limiter: waiting %.1fs before next request", wait_time)
            await asyncio.sleep(wait_time)
        return wait_time

    def _parse_reset(self, value: str) -> float | None:
        """Parse a reset timestamp. Could be ISO8601 or seconds-from-now."""
        try:
            return float(value)
        except ValueError:
            pass
        # Try ISO8601
        try:
            from datetime import datetime, timezone

            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, ImportError):
            return None

    @property
    def stats(self) -> dict:
        result: dict = {
            "is_throttled": self.is_throttled,
            "throttle_remaining_seconds": round(self.throttle_remaining_seconds, 2),
            "providers": {},
        }
        for provider, window in self._windows.items():
            result["providers"][provider] = {
                "remaining_requests": window.remaining_requests,
                "remaining_tokens": window.remaining_tokens,
                "limit_requests": window.limit_requests,
                "limit_tokens": window.limit_tokens,
                "age_seconds": round(time.time() - window.updated_at, 1),
            }
        return result
