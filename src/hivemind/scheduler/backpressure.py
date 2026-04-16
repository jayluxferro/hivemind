"""Backpressure controller — AIMD (Additive Increase Multiplicative Decrease).

Like TCP congestion control but for LLM API concurrency.
When latency is low → increase concurrency.
When latency spikes → cut concurrency.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class BackpressureController:
    """AIMD-based concurrency controller that reacts to API latency."""

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

        # History for metrics
        self._adjustments: deque[dict] = deque(maxlen=100)

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

    async def record_latency(self, latency_ms: float) -> None:
        """Record a request latency sample and maybe adjust concurrency."""
        self._window.append(latency_ms)
        now = time.monotonic()
        if now - self._last_update >= self._update_interval:
            await self._update_concurrency()
            self._last_update = now

    async def record_error(self) -> None:
        """Record an error (429, 502, ECONNRESET) — immediate backoff."""
        async with self._lock:
            self._error_count += 1
            old = self._current_concurrency
            self._current_concurrency = max(
                self._min, self._current_concurrency * self._md
            )
            logger.warning(
                "Backpressure: error detected, concurrency %.1f → %.1f",
                old,
                self._current_concurrency,
            )
            self._adjustments.append({
                "time": time.time(),
                "trigger": "error",
                "old": old,
                "new": self._current_concurrency,
            })

    async def record_success(self) -> None:
        """Record a successful request — gradual increase."""
        # Increase is handled in _update_concurrency based on latency
        pass

    async def _update_concurrency(self) -> None:
        """AIMD update based on average latency in the window."""
        async with self._lock:
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
                    avg_latency,
                    self._latency_target,
                    old,
                    self._current_concurrency,
                )
                self._adjustments.append({
                    "time": time.time(),
                    "trigger": "aimd",
                    "avg_latency_ms": avg_latency,
                    "old": old,
                    "new": self._current_concurrency,
                })

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
            "window_size": len(self._window),
            "recent_adjustments": list(self._adjustments)[-5:],
        }
