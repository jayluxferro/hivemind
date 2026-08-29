"""Admission controller — concurrency gate for API requests.

Uses a condition variable + manual counter instead of asyncio.Semaphore
to support safe dynamic resizing. The old implementation mutated
Semaphore._value directly, which is undefined behavior.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class AdmissionController:
    def __init__(self, max_concurrency: int = 5) -> None:
        # Zero or negative would make acquire() wait forever (active >= max with max==0).
        self._max = max(1, max_concurrency)
        self._active = 0
        self._total_admitted = 0
        self._total_queued_time = 0.0
        self._lock = asyncio.Lock()
        self._slot_available = asyncio.Condition(self._lock)

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
        avg_queue = self._total_queued_time / self._total_admitted if self._total_admitted > 0 else 0.0
        return {
            "max_concurrency": self._max,
            "active": self._active,
            "available": self.available,
            "total_admitted": self._total_admitted,
            "avg_queue_time_ms": round(avg_queue * 1000, 2),
        }

    async def set_max_concurrency(self, new_max: int) -> None:
        """Dynamically adjust max concurrency. Safe under concurrent load."""
        async with self._lock:
            old_max = self._max
            self._max = max(1, new_max)
            if self._max > old_max:
                # More slots available — wake all waiters so they can check
                self._slot_available.notify_all()
            logger.info("Admission: concurrency %d → %d", old_max, self._max)

    async def acquire(self, timeout: float | None = None) -> bool:
        """Acquire a slot. Returns True if acquired, False if timed out."""
        start = time.monotonic()
        deadline = start + timeout if timeout is not None else None

        async with self._lock:
            while self._active >= self._max:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("Admission: timed out waiting for slot (%.1fs)", timeout)
                        return False
                try:
                    await asyncio.wait_for(
                        self._slot_available.wait(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Admission: timed out waiting for slot (%.1fs)", timeout)
                    return False

            self._active += 1
            self._total_admitted += 1
            elapsed = time.monotonic() - start
            self._total_queued_time += elapsed

        if elapsed > 0.1:
            logger.debug(
                "Admission: acquired slot after %.1fs wait (active=%d/%d)",
                elapsed,
                self._active,
                self._max,
            )
        return True

    async def release(self) -> None:
        """Release a slot back to the pool."""
        async with self._lock:
            self._active = max(0, self._active - 1)
            self._slot_available.notify(1)

    async def __aenter__(self) -> AdmissionController:
        await self.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.release()
