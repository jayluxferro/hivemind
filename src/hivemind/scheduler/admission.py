"""Admission controller — concurrency semaphore for API requests.

The core primitive: don't let more than N concurrent requests hit the API.
N is measured (not guessed) and can be adjusted dynamically by the
backpressure controller.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class AdmissionController:
    def __init__(self, max_concurrency: int = 5) -> None:
        self._max = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._active = 0
        self._total_admitted = 0
        self._total_queued_time = 0.0
        self._lock = asyncio.Lock()

    @property
    def max_concurrency(self) -> int:
        return self._max

    @property
    def active(self) -> int:
        return self._active

    @property
    def available(self) -> int:
        return max(0, self._max - self._active)

    @property
    def stats(self) -> dict:
        avg_queue = (
            self._total_queued_time / self._total_admitted
            if self._total_admitted > 0
            else 0.0
        )
        return {
            "max_concurrency": self._max,
            "active": self._active,
            "available": self.available,
            "total_admitted": self._total_admitted,
            "avg_queue_time_ms": round(avg_queue * 1000, 2),
        }

    async def set_max_concurrency(self, new_max: int) -> None:
        """Dynamically adjust max concurrency (called by backpressure controller)."""
        async with self._lock:
            old_max = self._max
            self._max = max(1, new_max)
            delta = self._max - old_max

            if delta > 0:
                # Increase: release extra permits
                for _ in range(delta):
                    self._semaphore.release()
                logger.info("Admission: concurrency %d → %d (increased)", old_max, self._max)
            elif delta < 0:
                # Decrease: acquire permits (non-blocking best effort)
                # New requests will naturally be throttled as the semaphore drains
                for _ in range(-delta):
                    # Try to acquire without blocking — if we can't, that's fine,
                    # the reduction takes effect as slots free up
                    try:
                        self._semaphore._value = max(0, self._semaphore._value - 1)
                    except Exception:
                        break
                logger.info("Admission: concurrency %d → %d (decreased)", old_max, self._max)

    async def acquire(self, timeout: float | None = None) -> bool:
        """Acquire a slot. Returns True if acquired, False if timed out."""
        start = time.monotonic()
        try:
            if timeout is not None:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            else:
                await self._semaphore.acquire()
        except asyncio.TimeoutError:
            logger.warning("Admission: timed out waiting for slot (%.1fs)", timeout)
            return False

        elapsed = time.monotonic() - start
        async with self._lock:
            self._active += 1
            self._total_admitted += 1
            self._total_queued_time += elapsed

        if elapsed > 0.1:
            logger.debug("Admission: acquired slot after %.1fs wait (active=%d/%d)", elapsed, self._active, self._max)
        return True

    async def release(self) -> None:
        """Release a slot back to the pool."""
        async with self._lock:
            self._active = max(0, self._active - 1)
        self._semaphore.release()

    async def __aenter__(self) -> AdmissionController:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()
