"""Distributed scheduling — shared state across multiple HiveMind instances.

Uses Redis for coordination:
- Distributed semaphore for admission control
- Shared rate limit state
- Shared token budget counters

Falls back gracefully to local-only mode when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

logger = logging.getLogger(__name__)


class DistributedLock(Protocol):
    """Protocol for a distributed lock/semaphore."""

    async def acquire(self, timeout: float | None = None) -> bool: ...
    async def release(self) -> None: ...
    async def get_count(self) -> int: ...


class DistributedCounter(Protocol):
    """Protocol for a distributed atomic counter."""

    async def increment(self, amount: int = 1) -> int: ...
    async def get(self) -> int: ...
    async def set(self, value: int) -> None: ...


class RedisBackend:
    """Redis-based distributed coordination backend."""

    def __init__(self, redis_url: str = "redis://localhost:6379", prefix: str = "hivemind") -> None:
        self._redis_url = redis_url
        self._prefix = prefix
        self._redis = None
        self._connected = False

    async def connect(self) -> bool:
        """Connect to Redis. Returns False if unavailable."""
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            self._connected = True
            logger.info("Distributed: connected to Redis at %s", self._redis_url)
            return True
        except ImportError:
            logger.warning("Distributed: redis package not installed, using local-only mode")
            return False
        except Exception as exc:
            logger.warning("Distributed: Redis unavailable (%s), using local-only mode", exc)
            return False

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def _key(self, name: str) -> str:
        return f"{self._prefix}:{name}"

    # --- Distributed Semaphore ---

    async def semaphore_acquire(self, name: str, max_count: int, timeout: float | None = None) -> bool:
        """Acquire a slot in a distributed counting semaphore."""
        if not self._connected:
            return True  # Fallback: always allow

        key = self._key(f"sem:{name}")
        deadline = time.monotonic() + (timeout or 30.0)

        while time.monotonic() < deadline:
            # Atomic: increment if below max
            current = await self._redis.get(key)
            current = int(current) if current else 0

            if current < max_count:
                new_val = await self._redis.incr(key)
                if new_val <= max_count:
                    # Set TTL as safety net (auto-release if process dies)
                    await self._redis.expire(key, 300)
                    return True
                else:
                    # Race condition — someone else got the slot
                    await self._redis.decr(key)

            await asyncio.sleep(0.1)

        return False

    async def semaphore_release(self, name: str) -> None:
        """Release a slot in a distributed counting semaphore."""
        if not self._connected:
            return
        key = self._key(f"sem:{name}")
        val = await self._redis.decr(key)
        if val < 0:
            await self._redis.set(key, 0)

    async def semaphore_count(self, name: str) -> int:
        """Get current count of a distributed semaphore."""
        if not self._connected:
            return 0
        key = self._key(f"sem:{name}")
        val = await self._redis.get(key)
        return int(val) if val else 0

    # --- Distributed Counters ---

    async def counter_increment(self, name: str, amount: int = 1) -> int:
        """Atomically increment a counter."""
        if not self._connected:
            return 0
        key = self._key(f"counter:{name}")
        return await self._redis.incrby(key, amount)

    async def counter_get(self, name: str) -> int:
        """Get counter value."""
        if not self._connected:
            return 0
        key = self._key(f"counter:{name}")
        val = await self._redis.get(key)
        return int(val) if val else 0

    async def counter_set(self, name: str, value: int) -> None:
        """Set counter value."""
        if not self._connected:
            return
        key = self._key(f"counter:{name}")
        await self._redis.set(key, value)

    # --- Shared Rate Limit State ---

    async def set_rate_limit_state(self, provider: str, state: dict) -> None:
        """Store rate limit state for sharing across instances."""
        if not self._connected:
            return
        import json

        key = self._key(f"ratelimit:{provider}")
        await self._redis.set(key, json.dumps(state), ex=120)  # TTL 2 min

    async def get_rate_limit_state(self, provider: str) -> dict | None:
        """Get shared rate limit state."""
        if not self._connected:
            return None
        import json

        key = self._key(f"ratelimit:{provider}")
        val = await self._redis.get(key)
        if val:
            return json.loads(val)
        return None

    # --- Shared Pause/Throttle ---

    async def set_pause_until(self, until: float) -> None:
        """Signal all instances to pause until a timestamp."""
        if not self._connected:
            return
        key = self._key("pause_until")
        await self._redis.set(key, str(until), ex=120)

    async def get_pause_until(self) -> float:
        """Get the shared pause timestamp."""
        if not self._connected:
            return 0.0
        key = self._key("pause_until")
        val = await self._redis.get(key)
        return float(val) if val else 0.0

    @property
    def stats(self) -> dict:
        return {
            "backend": "redis" if self._connected else "local",
            "connected": self._connected,
            "redis_url": self._redis_url if self._connected else None,
        }


class LocalBackend:
    """Local-only fallback when Redis is not available. Uses in-process state."""

    def __init__(self) -> None:
        self._connected = True
        self._semaphores: dict[str, int] = {}
        self._counters: dict[str, int] = {}
        self._rate_limit_state: dict[str, dict] = {}
        self._pause_until: float = 0.0

    async def connect(self) -> bool:
        return True

    async def close(self) -> None:
        pass

    @property
    def connected(self) -> bool:
        return True

    async def semaphore_acquire(self, name: str, max_count: int, timeout: float | None = None) -> bool:
        current = self._semaphores.get(name, 0)
        if current < max_count:
            self._semaphores[name] = current + 1
            return True
        return False

    async def semaphore_release(self, name: str) -> None:
        current = self._semaphores.get(name, 0)
        self._semaphores[name] = max(0, current - 1)

    async def semaphore_count(self, name: str) -> int:
        return self._semaphores.get(name, 0)

    async def counter_increment(self, name: str, amount: int = 1) -> int:
        val = self._counters.get(name, 0) + amount
        self._counters[name] = val
        return val

    async def counter_get(self, name: str) -> int:
        return self._counters.get(name, 0)

    async def counter_set(self, name: str, value: int) -> None:
        self._counters[name] = value

    async def set_rate_limit_state(self, provider: str, state: dict) -> None:
        self._rate_limit_state[provider] = state

    async def get_rate_limit_state(self, provider: str) -> dict | None:
        return self._rate_limit_state.get(provider)

    async def set_pause_until(self, until: float) -> None:
        self._pause_until = until

    async def get_pause_until(self) -> float:
        return self._pause_until

    @property
    def stats(self) -> dict:
        return {"backend": "local", "connected": True}


async def create_backend(redis_url: str | None = None) -> RedisBackend | LocalBackend:
    """Create the appropriate distributed backend.

    Tries Redis if URL provided, falls back to local.
    """
    if redis_url:
        backend = RedisBackend(redis_url)
        if await backend.connect():
            return backend
    return LocalBackend()
