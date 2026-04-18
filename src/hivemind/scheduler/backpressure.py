"""Backpressure controller — AIMD-inspired concurrency adjustment.

Adapts TCP congestion control principles for LLM API concurrency:
- Additive increase when latency is below target
- Multiplicative decrease on errors or high latency

Honest framing: this is AIMD-*inspired*, not a faithful TCP Reno
implementation. Key differences from TCP:
- No slow start phase (APIs have known baseline concurrency)
- No RTT-based timing (uses wall-clock update interval)
- No congestion avoidance / fast recovery phases
- Concurrency adjustments are directly applied to admission controller

The controller holds a reference to the AdmissionController and
directly applies its recommendations — no disconnected state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class BackpressureController:
    """AIMD-inspired concurrency controller that reacts to API latency."""

    def __init__(
        self,
        max_concurrency: int = 5,
        *,
        additive_increase: float = 0.5,
        multiplicative_decrease: float = 0.5,
        latency_target_ms: float = 2000.0,
        min_concurrency: int = 1,
        window_size: int = 20,
        update_interval: float = 5.0,
        admission: object | None = None,  # AdmissionController, avoids circular import
    ) -> None:
        self._max_concurrency = max_concurrency
        self._current_concurrency = float(max_concurrency)
        self._ai = additive_increase
        self._md = multiplicative_decrease
        self._latency_target = latency_target_ms
        self._min = min_concurrency
        self._window: deque[float] = deque(maxlen=window_size)
        self._update_interval = update_interval
        self._last_update = 0.0
        self._error_count = 0
        self._lock = asyncio.Lock()
        self._admission = admission  # Direct reference to admission controller

        # Circuit breaker state
        self._recent_errors = 0
        self._recent_requests = 0
        self._circuit_open = False
        self._circuit_open_until = 0.0
        self._circuit_error_threshold = 0.5  # Trip at 50% error rate
        self._circuit_window_size = 20
        self._circuit_cooldown = 10.0  # Seconds before half-open

        # History for metrics
        self._adjustments: deque[dict] = deque(maxlen=100)

    def set_admission(self, admission) -> None:
        """Set the admission controller reference (for deferred wiring)."""
        self._admission = admission

    @property
    def recommended_concurrency(self) -> int:
        return max(self._min, int(self._current_concurrency))

    @property
    def current_avg_latency_ms(self) -> float:
        if not self._window:
            return 0.0
        return sum(self._window) / len(self._window)

    @property
    def backpressure_factor(self) -> float:
        """0.0 = no pressure (full speed), 1.0 = max pressure (min concurrency)."""
        if self._max_concurrency <= self._min:
            return 0.0
        effective = self._current_concurrency - self._min
        range_ = self._max_concurrency - self._min
        return 1.0 - (effective / range_)

    @property
    def circuit_open(self) -> bool:
        if self._circuit_open and time.monotonic() > self._circuit_open_until:
            # Half-open: allow a probe
            return False
        return self._circuit_open

    async def record_latency(self, latency_ms: float) -> None:
        """Record a request latency sample and maybe adjust concurrency."""
        async with self._lock:
            self._window.append(latency_ms)
            self._recent_requests += 1
            now = time.monotonic()
            if now - self._last_update >= self._update_interval:
                await self._update_concurrency()
                self._last_update = now

    async def record_error(self) -> None:
        """Record an error (429, 502, ECONNRESET) — immediate backoff."""
        async with self._lock:
            self._error_count += 1
            self._recent_errors += 1
            self._recent_requests += 1
            old = self._current_concurrency
            self._current_concurrency = max(
                self._min, self._current_concurrency * self._md
            )
            self._adjustments.append({
                "time": time.time(),
                "trigger": "error",
                "old": old,
                "new": self._current_concurrency,
            })
            logger.warning(
                "Backpressure: error detected, concurrency %.1f → %.1f",
                old, self._current_concurrency,
            )

            # Circuit breaker check
            if self._recent_requests >= self._circuit_window_size:
                error_rate = self._recent_errors / self._recent_requests
                if error_rate >= self._circuit_error_threshold:
                    self._circuit_open = True
                    self._circuit_open_until = time.monotonic() + self._circuit_cooldown
                    logger.error(
                        "Backpressure: CIRCUIT OPEN — %.0f%% error rate (%d/%d), "
                        "cooling down %.0fs",
                        error_rate * 100, self._recent_errors,
                        self._recent_requests, self._circuit_cooldown,
                    )
                self._recent_errors = 0
                self._recent_requests = 0

            # Apply to admission controller
            await self._apply_to_admission()

    async def record_success(self) -> None:
        """Record a successful request. Resets circuit breaker if half-open."""
        async with self._lock:
            self._recent_requests += 1
            if self._circuit_open and time.monotonic() > self._circuit_open_until:
                # Half-open probe succeeded — close circuit
                self._circuit_open = False
                logger.info("Backpressure: circuit CLOSED after successful probe")

    async def _update_concurrency(self) -> None:
        """AIMD update based on average latency in the window."""
        if not self._window:
            return

        avg_latency = self.current_avg_latency_ms
        old = self._current_concurrency

        if avg_latency <= self._latency_target:
            # Additive increase
            self._current_concurrency = min(
                self._max_concurrency,
                self._current_concurrency + self._ai,
            )
        else:
            # Multiplicative decrease
            self._current_concurrency = max(
                self._min,
                self._current_concurrency * self._md,
            )

        if int(old) != int(self._current_concurrency):
            logger.info(
                "Backpressure: AIMD adjust — avg_latency=%.0fms target=%.0fms, "
                "concurrency %.1f → %.1f",
                avg_latency, self._latency_target, old, self._current_concurrency,
            )
            self._adjustments.append({
                "time": time.time(),
                "trigger": "aimd",
                "avg_latency_ms": avg_latency,
                "old": old,
                "new": self._current_concurrency,
            })

            # Apply to admission controller
            await self._apply_to_admission()

    async def _apply_to_admission(self) -> None:
        """Push concurrency recommendation to the admission controller."""
        if self._admission is not None:
            recommended = self.recommended_concurrency
            try:
                await self._admission.set_max_concurrency(recommended)
            except Exception as exc:
                logger.warning("Backpressure: failed to update admission: %s", exc)

    @property
    def stats(self) -> dict:
        return {
            "recommended_concurrency": self.recommended_concurrency,
            "current_concurrency_float": round(self._current_concurrency, 2),
            "max_concurrency": self._max_concurrency,
            "min_concurrency": self._min,
            "avg_latency_ms": round(self.current_avg_latency_ms, 2),
            "latency_target_ms": self._latency_target,
            "backpressure_factor": round(self.backpressure_factor, 3),
            "error_count": self._error_count,
            "circuit_open": self._circuit_open,
            "window_size": len(self._window),
            "recent_adjustments": list(self._adjustments)[-5:],
        }
