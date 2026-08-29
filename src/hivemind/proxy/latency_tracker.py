"""Latency tracker — rolling measurement of API response times.

Feeds the backpressure controller with latency samples.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class LatencySample:
    latency_ms: float
    status_code: int | None = None
    recorded_at: float = field(default_factory=time.time)


class LatencyTracker:
    """Rolling window latency tracker."""

    def __init__(self, window_size: int = 100) -> None:
        self._samples: deque[LatencySample] = deque(maxlen=window_size)
        self._total_requests = 0

    def record(self, latency_ms: float, status_code: int | None = None) -> None:
        self._samples.append(LatencySample(latency_ms=latency_ms, status_code=status_code))
        self._total_requests += 1

    @property
    def avg_ms(self) -> float:
        if not self._samples:
            return 0.0
        return sum(s.latency_ms for s in self._samples) / len(self._samples)

    @property
    def p50_ms(self) -> float:
        return self._percentile(0.5)

    @property
    def p95_ms(self) -> float:
        return self._percentile(0.95)

    @property
    def p99_ms(self) -> float:
        return self._percentile(0.99)

    @property
    def max_ms(self) -> float:
        if not self._samples:
            return 0.0
        return max(s.latency_ms for s in self._samples)

    @property
    def min_ms(self) -> float:
        if not self._samples:
            return 0.0
        return min(s.latency_ms for s in self._samples)

    @property
    def error_rate(self) -> float:
        if not self._samples:
            return 0.0
        errors = sum(1 for s in self._samples if s.status_code is not None and s.status_code >= 400)
        return errors / len(self._samples)

    def _percentile(self, p: float) -> float:
        if not self._samples:
            return 0.0
        sorted_latencies = sorted(s.latency_ms for s in self._samples)
        idx = int(len(sorted_latencies) * p)
        idx = min(idx, len(sorted_latencies) - 1)
        return sorted_latencies[idx]

    @property
    def stats(self) -> dict:
        return {
            "total_requests": self._total_requests,
            "window_size": len(self._samples),
            "avg_ms": round(self.avg_ms, 2),
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "p99_ms": round(self.p99_ms, 2),
            "min_ms": round(self.min_ms, 2),
            "max_ms": round(self.max_ms, 2),
            "error_rate": round(self.error_rate, 4),
        }
