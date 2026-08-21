"""Rate limit tracker — parses provider headers and proactively throttles.

Reads x-ratelimit-* headers from API responses and pauses requests
before hitting limits, not after. Can be pre-seeded from a provider
profile so throttling works before the first response arrives.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
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
    """Tracks rate limit state from API response headers and gates requests.

    Two layers of protection:
    1. Header-based: parses x-ratelimit-* / anthropic-ratelimit-* headers
       from API responses and throttles proactively when near limits.
    2. Request counter: a sliding-window RPM counter seeded from the
       provider profile. Acts as a safety net when the provider sends
       no rate-limit headers (e.g. Ollama) or before the first response.
    """

    def __init__(self) -> None:
        self._windows: dict[str, RateLimitWindow] = {}
        self._lock = asyncio.Lock()
        # Threshold: if remaining requests < this fraction of limit, start throttling
        self._throttle_threshold = 0.1
        self._pause_until: float = 0.0

        # Sliding-window RPM counter (provider-seeded)
        self._rpm_limit: int | None = None  # None = no RPM enforcement
        self._tpm_limit: int | None = None
        self._request_timestamps: deque[float] = deque()
        self._token_usage: deque[tuple[float, int]] = deque()  # (timestamp, tokens)
        self._provider_name: str | None = None

    def configure_from_profile(self, profile) -> None:
        """Pre-seed rate limits from a ProviderProfile.

        Sets up the sliding-window RPM/TPM counters so the rate limiter
        can throttle before any response headers arrive.
        """
        self._rpm_limit = profile.default_requests_per_minute
        self._tpm_limit = profile.default_tokens_per_minute
        self._provider_name = profile.name
        logger.info(
            "Rate limiter: configured for %s — %d RPM, %d TPM",
            profile.name, self._rpm_limit, self._tpm_limit,
        )

    def apply_overrides(self, *, rpm: int | None = None, tpm: int | None = None) -> None:
        """Apply explicit CLI/config limits on top of the provider profile.

        Only non-None values are overridden. Call after configure_from_profile.
        """
        if rpm is not None:
            self._rpm_limit = rpm
        if tpm is not None:
            self._tpm_limit = tpm
        if rpm is not None or tpm is not None:
            logger.info(
                "Rate limiter: overrides applied — %s RPM, %s TPM",
                self._rpm_limit, self._tpm_limit,
            )

    def record_request(self) -> None:
        """Record that a request was sent (for RPM counting)."""
        self._request_timestamps.append(time.monotonic())

    def record_tokens(self, count: int) -> None:
        """Record token usage (for TPM counting)."""
        if count > 0:
            self._token_usage.append((time.monotonic(), count))

    def _rpm_wait_seconds(self) -> float:
        """How long to wait based on RPM counter. 0 = no wait needed."""
        if self._rpm_limit is None:
            return 0.0
        now = time.monotonic()
        window = 60.0
        # Evict old entries
        while self._request_timestamps and now - self._request_timestamps[0] > window:
            self._request_timestamps.popleft()
        if len(self._request_timestamps) >= self._rpm_limit:
            # Wait until the oldest request in the window expires
            oldest = self._request_timestamps[0]
            return (oldest + window) - now
        return 0.0

    def _tpm_wait_seconds(self) -> float:
        """How long to wait based on TPM counter. 0 = no wait needed."""
        if self._tpm_limit is None:
            return 0.0
        now = time.monotonic()
        window = 60.0
        # Evict old entries
        while self._token_usage and now - self._token_usage[0][0] > window:
            self._token_usage.popleft()
        total = sum(t for _, t in self._token_usage)
        if total >= self._tpm_limit:
            oldest_ts = self._token_usage[0][0]
            return (oldest_ts + window) - now
        return 0.0

    @property
    def is_throttled(self) -> bool:
        return time.time() < self._pause_until or self._rpm_wait_seconds() > 0 or self._tpm_wait_seconds() > 0

    @property
    def throttle_remaining_seconds(self) -> float:
        header_wait = max(0.0, self._pause_until - time.time())
        return max(header_wait, self._rpm_wait_seconds(), self._tpm_wait_seconds())

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
        """Block until we're allowed to make a request. Returns seconds waited.

        Also records the request in the RPM sliding window.
        """
        total_waited = 0.0
        while True:
            wait_time = self.throttle_remaining_seconds
            if wait_time <= 0:
                break
            logger.info("Rate limiter: waiting %.1fs before next request", wait_time)
            await asyncio.sleep(wait_time)
            total_waited += wait_time
        self.record_request()
        return total_waited

    def _parse_reset(self, value: str) -> float | None:
        """Parse a reset timestamp. Could be ISO8601 or seconds-from-now."""
        try:
            return float(value)
        except ValueError:
            pass
        # Try ISO8601
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, ImportError):
            return None

    @property
    def stats(self) -> dict:
        now = time.monotonic()
        # Count requests in the last 60s
        while self._request_timestamps and now - self._request_timestamps[0] > 60:
            self._request_timestamps.popleft()
        while self._token_usage and now - self._token_usage[0][0] > 60:
            self._token_usage.popleft()

        result: dict = {
            "is_throttled": self.is_throttled,
            "throttle_remaining_seconds": round(self.throttle_remaining_seconds, 2),
            "provider": self._provider_name,
            "rpm_limit": self._rpm_limit,
            "rpm_current": len(self._request_timestamps),
            "tpm_limit": self._tpm_limit,
            "tpm_current": sum(t for _, t in self._token_usage),
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
