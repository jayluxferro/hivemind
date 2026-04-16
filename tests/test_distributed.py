"""Tests for distributed scheduling backend."""

import pytest

from hivemind.scheduler.distributed import LocalBackend, create_backend


@pytest.mark.asyncio
async def test_local_backend_semaphore():
    backend = LocalBackend()
    assert await backend.semaphore_acquire("test", max_count=2) is True
    assert await backend.semaphore_acquire("test", max_count=2) is True
    assert await backend.semaphore_acquire("test", max_count=2) is False  # Full

    await backend.semaphore_release("test")
    assert await backend.semaphore_acquire("test", max_count=2) is True

    assert await backend.semaphore_count("test") == 2


@pytest.mark.asyncio
async def test_local_backend_counter():
    backend = LocalBackend()
    assert await backend.counter_get("tokens") == 0

    val = await backend.counter_increment("tokens", 100)
    assert val == 100

    val = await backend.counter_increment("tokens", 50)
    assert val == 150

    assert await backend.counter_get("tokens") == 150

    await backend.counter_set("tokens", 0)
    assert await backend.counter_get("tokens") == 0


@pytest.mark.asyncio
async def test_local_backend_rate_limit_state():
    backend = LocalBackend()
    assert await backend.get_rate_limit_state("anthropic") is None

    await backend.set_rate_limit_state("anthropic", {"remaining": 45})
    state = await backend.get_rate_limit_state("anthropic")
    assert state["remaining"] == 45


@pytest.mark.asyncio
async def test_local_backend_pause():
    backend = LocalBackend()
    assert await backend.get_pause_until() == 0.0

    await backend.set_pause_until(9999999999.0)
    assert await backend.get_pause_until() == 9999999999.0


@pytest.mark.asyncio
async def test_create_backend_no_redis():
    # Without Redis URL, should return LocalBackend
    backend = await create_backend(redis_url=None)
    assert isinstance(backend, LocalBackend)


@pytest.mark.asyncio
async def test_create_backend_bad_redis():
    # Invalid Redis URL should fall back to LocalBackend
    backend = await create_backend(redis_url="redis://nonexistent:9999")
    assert isinstance(backend, LocalBackend)


@pytest.mark.asyncio
async def test_local_backend_stats():
    backend = LocalBackend()
    stats = backend.stats
    assert stats["backend"] == "local"
    assert stats["connected"] is True
