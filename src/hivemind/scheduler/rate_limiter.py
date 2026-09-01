"""Rate limit tracker — parses provider headers and proactively throttles.

Reads x-ratelimit-* headers from API responses and pauses requests
before hitting limits, not after. Can be pre-seeded from a provider
profile so throttling works before the first response arrives.

Two scopes for the local sliding-window counters (see ``scope``):

* ``per_agent`` (default) — each agent_id gets its own RPM/TPM window, so
  one busy session cannot consume another session's self-imposed budget.
  Calls with no agent_id fall back to the shared global window.
* ``global`` — every request shares the single global window (the original
  behavior; select with ``--rate-limit-scope global``).

Header-driven state is ALWAYS global regardless of scope: 429s, retry-after,
and proactive throttling reflect the upstream provider's view of the shared
API key, so they must pause everyone.

Aggregate pressure in per_agent scope is handled by the fair-share governor
rather than a hard shared window. A global counter held at the same RPM/TPM
values would throttle every agent as soon as their combined traffic hit the
limit — exactly the cross-agent stall per-agent scoping exists to remove.
Instead, when combined in-window traffic exceeds the provider limit, every
bucket's effective limit shrinks by the stateless factor limit/aggregate:
heavy agents exceed the shrunken limit first and absorb the squeeze, light
agents barely feel it, and the factor returns to 1.0 as the window drains
(there is no AIMD state to get stuck low). The provider's true ceiling still
arrives via response headers (above), pausing everyone when the key is truly
saturated.

Per-agent limit overrides (``agent_limits``) cap specific agents below the
provider defaults — e.g. pin a background batch agent to 20 RPM while
interactive sessions keep 50. Overrides are caps, not guarantees: under
provider-key saturation the governor shrinks every bucket proportionally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Length of the sliding window for local RPM/TPM counters.
_WINDOW_SECONDS = 60.0

# Maximum total time a request may sit in the rate-limiter queue.  Beyond
# this the interceptor fails FAST with a 429 + retry-after instead of
# letting the queue stretch for minutes (a deep burst queue once hit ~300s
# — every layer's read ceiling — and surfaced as a bare gateway ReadTimeout).
MAX_WAIT_S = 60.0


class ThrottleWaitExceeded(Exception):
    """Raised by wait_if_throttled when the projected queue wait exceeds MAX_WAIT_S."""

    def __init__(self, wait_s: float) -> None:
        super().__init__(f"rate-limit queue would wait {wait_s:.0f}s (> {MAX_WAIT_S:.0f}s)")
        self.wait_s = wait_s


# Valid values for the rate-limiter scope. "per_agent" buckets the local
# sliding windows by agent identity; "global" keeps one shared window.
SCOPES = ("per_agent", "global")

# Limit kinds allowed in per-agent overrides.
AGENT_LIMIT_KINDS = ("rpm", "tpm")


def validate_agent_limits(overrides: dict) -> dict[str, dict[str, int]]:
    """Validate and normalize a per-agent limit-override registry.

    Shape: ``{agent_id: {"rpm": int, "tpm": int}}`` — either key optional,
    values positive ints. Fails loudly: a silently ignored typo would leave
    an agent unthrottled against the shared provider key.
    """
    if not isinstance(overrides, dict):
        raise ValueError(f"agent_limit_overrides must be a dict, got {type(overrides).__name__}")
    normalized: dict[str, dict[str, int]] = {}
    for agent_id, limits in overrides.items():
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError(f"agent_limit_overrides keys must be non-empty strings, got {agent_id!r}")
        if not isinstance(limits, dict):
            raise ValueError(f"agent_limit_overrides[{agent_id!r}] must be a dict, got {type(limits).__name__}")
        entry: dict[str, int] = {}
        for kind, value in limits.items():
            if kind not in AGENT_LIMIT_KINDS:
                raise ValueError(
                    f"agent_limit_overrides[{agent_id!r}] has unknown limit kind {kind!r}; "
                    f"expected one of {AGENT_LIMIT_KINDS}"
                )
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"agent_limit_overrides[{agent_id!r}][{kind!r}] must be a positive int, got {value!r}")
            entry[kind] = value
        if not entry:
            raise ValueError(
                f"agent_limit_overrides[{agent_id!r}] is empty; expected at least one of {AGENT_LIMIT_KINDS}"
            )
        normalized[agent_id.strip()] = entry
    return normalized


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

    Three layers of protection:
    1. Header-based: parses x-ratelimit-* / anthropic-ratelimit-* headers
       from API responses and throttles proactively when near limits.
       This layer is always global — it reflects the shared API key.
    2. Request counter: a sliding-window RPM/TPM counter seeded from the
       provider profile. Acts as a safety net when the provider sends
       no rate-limit headers (e.g. Ollama) or before the first response.
       This layer is scoped: per-agent by default, or global when
       ``scope="global"``. The ``agent_limits`` registry can cap
       individual agents below the configured defaults.
    3. Fair-share governor (per_agent scope only): when combined in-window
       traffic exceeds the provider limit, every bucket's effective limit
       is scaled by limit/aggregate — contention costs the heaviest agents
       first. Stateless: recomputed from the windows on every check.
    """

    def __init__(self, *, scope: str = "per_agent", agent_limits: dict | None = None) -> None:
        if scope not in SCOPES:
            raise ValueError(f"Invalid rate limiter scope {scope!r}; expected one of {SCOPES}")
        self._scope = scope
        self._windows: dict[str, RateLimitWindow] = {}
        self._lock = asyncio.Lock()
        # Threshold: if remaining requests < this fraction of limit, start throttling
        self._throttle_threshold = 0.1
        self._pause_until: float = 0.0

        # Sliding-window RPM/TPM counters (provider-seeded)
        self._rpm_limit: int | None = None  # None = no RPM enforcement
        self._tpm_limit: int | None = None
        # Global windows: used for scope="global", for calls with no agent_id,
        # and directly by tests. Kept as plain attributes for back-compat.
        self._request_timestamps: deque[float] = deque()
        self._token_usage: deque[tuple[float, int]] = deque()  # (timestamp, tokens)
        # Per-agent windows, keyed by agent_id (scope="per_agent" only).
        self._agent_requests: dict[str, deque[float]] = {}
        self._agent_tokens: dict[str, deque[tuple[float, int]]] = {}
        # Per-agent limit overrides: agent_id -> {"rpm"/"tpm": positive int}.
        # Validated eagerly so a bad registry fails at construction, not
        # mid-traffic.
        self._agent_limits: dict[str, dict[str, int]] = validate_agent_limits(agent_limits or {})
        self._provider_name: str | None = None

    @property
    def scope(self) -> str:
        return self._scope

    def set_scope(self, scope: str) -> None:
        """Switch between per-agent and global windowing at runtime."""
        if scope not in SCOPES:
            raise ValueError(f"Invalid rate limiter scope {scope!r}; expected one of {SCOPES}")
        if scope != self._scope:
            logger.info("Rate limiter: scope %s -> %s", self._scope, scope)
            self._scope = scope

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
            profile.name,
            self._rpm_limit,
            self._tpm_limit,
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
                self._rpm_limit,
                self._tpm_limit,
            )

    def set_agent_limits(self, overrides: dict) -> None:
        """Replace the per-agent limit-override registry (validated, loud)."""
        self._agent_limits = validate_agent_limits(overrides)
        logger.info("Rate limiter: per-agent overrides set for %d agent(s)", len(self._agent_limits))

    def _limit_for(self, agent_id: str | None, kind: str) -> int | None:
        """An agent's base limit: override registry first, configured default else."""
        if agent_id is not None:
            override = self._agent_limits.get(agent_id)
            if override is not None and kind in override:
                return override[kind]
        return self._rpm_limit if kind == "rpm" else self._tpm_limit

    # -- scoped window selection -------------------------------------------

    def _requests_window(self, agent_id: str | None) -> deque[float]:
        """The request-timestamp deque an agent's traffic counts against."""
        if self._scope == "per_agent" and agent_id is not None:
            return self._agent_requests.setdefault(agent_id, deque())
        return self._request_timestamps

    def _tokens_window(self, agent_id: str | None) -> deque[tuple[float, int]]:
        """The token-usage deque an agent's traffic counts against."""
        if self._scope == "per_agent" and agent_id is not None:
            return self._agent_tokens.setdefault(agent_id, deque())
        return self._token_usage

    # -- recording ----------------------------------------------------------

    def record_request(self, agent_id: str | None = None) -> None:
        """Record that a request was sent (for RPM counting)."""
        self._requests_window(agent_id).append(time.monotonic())

    def record_tokens(self, count: int, agent_id: str | None = None) -> None:
        """Record token usage (for TPM counting)."""
        if count > 0:
            self._tokens_window(agent_id).append((time.monotonic(), count))

    # -- wait computation ----------------------------------------------------

    @staticmethod
    def _requests_wait(timestamps: deque[float], limit: int | None) -> float:
        """Seconds until the request window has capacity. 0 = no wait."""
        if limit is None:
            return 0.0
        now = time.monotonic()
        # Evict old entries
        while timestamps and now - timestamps[0] > _WINDOW_SECONDS:
            timestamps.popleft()
        if len(timestamps) >= limit:
            # Wait until the oldest request in the window expires
            return (timestamps[0] + _WINDOW_SECONDS) - now
        return 0.0

    @staticmethod
    def _tokens_wait(usage: deque[tuple[float, int]], limit: int | None) -> float:
        """Seconds until the token window has capacity. 0 = no wait."""
        if limit is None:
            return 0.0
        now = time.monotonic()
        # Evict old entries
        while usage and now - usage[0][0] > _WINDOW_SECONDS:
            usage.popleft()
        total = sum(t for _, t in usage)
        if total >= limit:
            return (usage[0][0] + _WINDOW_SECONDS) - now
        return 0.0

    @staticmethod
    def _count_requests(timestamps: deque[float], now: float) -> int:
        """In-window request count (non-mutating; pruning lives elsewhere)."""
        return sum(1 for ts in timestamps if now - ts <= _WINDOW_SECONDS)

    @staticmethod
    def _sum_tokens(usage: deque[tuple[float, int]], now: float) -> int:
        """In-window token total (non-mutating; pruning lives elsewhere)."""
        return sum(t for ts, t in usage if now - ts <= _WINDOW_SECONDS)

    def _aggregate_counts(self) -> tuple[int, int]:
        """In-window (requests, tokens) summed across ALL buckets.

        The fair-share governor measures aggregate pressure on the shared
        provider key from this — the global deque plus every per-agent deque.
        """
        now = time.monotonic()
        requests = self._count_requests(self._request_timestamps, now)
        tokens = self._sum_tokens(self._token_usage, now)
        for dq in self._agent_requests.values():
            requests += self._count_requests(dq, now)
        for dq in self._agent_tokens.values():
            tokens += self._sum_tokens(dq, now)
        return requests, tokens

    def _fair_share_factor(self, aggregate: int, provider_limit: int | None) -> float:
        """Proportional squeeze when combined traffic exceeds the provider limit.

        Pure function of current window state: 1.0 while the shared key has
        headroom, ``provider_limit / aggregate`` when over. Self-heals as the
        window drains — no AIMD state to get stuck low. Inactive in global
        scope, where the single shared window already caps the aggregate.
        """
        if self._scope != "per_agent" or provider_limit is None or aggregate <= provider_limit:
            return 1.0
        return provider_limit / aggregate

    def _governed_limit(self, base: int | None, provider_limit: int | None, aggregate: int) -> int | None:
        """Shrink an agent's base limit by the fair-share factor under contention."""
        if base is None:
            return None
        factor = self._fair_share_factor(aggregate, provider_limit)
        if factor >= 1.0:
            return base
        return max(1, int(base * factor))

    def _rpm_wait_seconds(self, agent_id: str | None = None) -> float:
        """How long to wait based on RPM counter. 0 = no wait needed."""
        aggregate, _ = self._aggregate_counts()
        limit = self._governed_limit(self._limit_for(agent_id, "rpm"), self._rpm_limit, aggregate)
        return self._requests_wait(self._requests_window(agent_id), limit)

    def _tpm_wait_seconds(self, agent_id: str | None = None) -> float:
        """How long to wait based on TPM counter. 0 = no wait needed."""
        _, aggregate = self._aggregate_counts()
        limit = self._governed_limit(self._limit_for(agent_id, "tpm"), self._tpm_limit, aggregate)
        return self._tokens_wait(self._tokens_window(agent_id), limit)

    def _wait_seconds(self, agent_id: str | None) -> float:
        """Combined wait: global header pause + this agent's window wait."""
        header_wait = max(0.0, self._pause_until - time.time())  # always global
        return max(header_wait, self._rpm_wait_seconds(agent_id), self._tpm_wait_seconds(agent_id))

    @property
    def is_throttled(self) -> bool:
        """Global view: header pause or the shared window saturated."""
        return self._wait_seconds(None) > 0

    def agent_is_throttled(self, agent_id: str | None) -> bool:
        """Scoped view: would a request from this agent have to wait?"""
        return self._wait_seconds(agent_id) > 0

    @property
    def throttle_remaining_seconds(self) -> float:
        """Global view: seconds until the shared window has capacity."""
        return self._wait_seconds(None)

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

        Header state is global: it describes the shared upstream API key,
        so it pauses every agent regardless of scope.
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

    async def wait_if_throttled(self, agent_id: str | None = None) -> float:
        """Block until we're allowed to make a request. Returns seconds waited.

        Waits on the global header pause plus this agent's scoped window,
        then records the request in that window.

        Raises :exc:`ThrottleWaitExceeded` when the projected wait exceeds
        :data:`MAX_WAIT_S` — beyond that the client is better served by a
        fast 429 (retry later) than by a minute-long silent queue.  A deep
        burst queue previously surfaced as a bare ReadTimeout at the
        gateway (2026-09-01: ~300s queue == every layer's 300s ceiling).
        """
        total_waited = 0.0
        while True:
            wait_time = self._wait_seconds(agent_id)
            if wait_time <= 0:
                break
            if total_waited + wait_time > MAX_WAIT_S:
                raise ThrottleWaitExceeded(total_waited + wait_time)
            logger.info(
                "Rate limiter: waiting %.1fs before next request (agent=%s)",
                wait_time,
                agent_id or "global",
            )
            await asyncio.sleep(wait_time)
            total_waited += wait_time
        self.record_request(agent_id)
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

    def _prune_agent_windows(self, now: float) -> None:
        """Drop agent windows with no traffic left in the sliding window."""
        for agent_id in [a for a, dq in self._agent_requests.items() if not dq or now - dq[-1] > _WINDOW_SECONDS]:
            del self._agent_requests[agent_id]
        for agent_id in [a for a, dq in self._agent_tokens.items() if not dq or now - dq[-1][0] > _WINDOW_SECONDS]:
            del self._agent_tokens[agent_id]

    @property
    def stats(self) -> dict:
        now = time.monotonic()
        # Count requests in the last 60s
        while self._request_timestamps and now - self._request_timestamps[0] > _WINDOW_SECONDS:
            self._request_timestamps.popleft()
        while self._token_usage and now - self._token_usage[0][0] > _WINDOW_SECONDS:
            self._token_usage.popleft()
        self._prune_agent_windows(now)
        aggregate_requests, aggregate_tokens = self._aggregate_counts()

        result: dict = {
            "is_throttled": self.is_throttled,
            "throttle_remaining_seconds": round(self.throttle_remaining_seconds, 2),
            "provider": self._provider_name,
            "scope": self._scope,
            "rpm_limit": self._rpm_limit,
            "rpm_current": len(self._request_timestamps),
            "tpm_limit": self._tpm_limit,
            "tpm_current": sum(t for _, t in self._token_usage),
            "fair_share": {
                "requests_factor": round(self._fair_share_factor(aggregate_requests, self._rpm_limit), 3),
                "tokens_factor": round(self._fair_share_factor(aggregate_tokens, self._tpm_limit), 3),
            },
            "agent_limits": {agent: dict(limits) for agent, limits in self._agent_limits.items()},
            "providers": {},
            "agents": {},
        }
        for agent_id, timestamps in self._agent_requests.items():
            while timestamps and now - timestamps[0] > _WINDOW_SECONDS:
                timestamps.popleft()
            tokens = self._agent_tokens.get(agent_id)
            token_total = 0
            if tokens:
                while tokens and now - tokens[0][0] > _WINDOW_SECONDS:
                    tokens.popleft()
                token_total = sum(t for _, t in tokens)
            result["agents"][agent_id] = {
                "rpm_current": len(timestamps),
                "tpm_current": token_total,
                "rpm_limit": self._limit_for(agent_id, "rpm"),
                "tpm_limit": self._limit_for(agent_id, "tpm"),
                "is_throttled": self.agent_is_throttled(agent_id),
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
