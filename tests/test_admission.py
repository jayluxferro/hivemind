"""Tests for the admission controller (concurrency semaphore)."""

import asyncio
import pytest

from hivemind.scheduler.admission import AdmissionController


def test_max_concurrency_zero_clamps_to_one():
    ac = AdmissionController(max_concurrency=0)
    assert ac.max_concurrency == 1


@pytest.mark.asyncio
async def test_basic_acquire_release():
    ac = AdmissionController(max_concurrency=3)
    assert ac.active == 0
    assert ac.available == 3

    await ac.acquire()
    assert ac.active == 1
    assert ac.available == 2

    await ac.release()
    assert ac.active == 0
    assert ac.available == 3


@pytest.mark.asyncio
async def test_context_manager():
    ac = AdmissionController(max_concurrency=2)
    async with ac:
        assert ac.active == 1
    assert ac.active == 0


@pytest.mark.asyncio
async def test_concurrency_limit():
    ac = AdmissionController(max_concurrency=2)

    await ac.acquire()
    await ac.acquire()
    assert ac.active == 2
    assert ac.available == 0

    # Third acquire should block — test with timeout
    result = await ac.acquire(timeout=0.1)
    assert result is False
    assert ac.active == 2

    # Release one — now acquire should succeed
    await ac.release()
    result = await ac.acquire(timeout=1.0)
    assert result is True
    assert ac.active == 2

    await ac.release()
    await ac.release()


@pytest.mark.asyncio
async def test_dynamic_concurrency_increase():
    ac = AdmissionController(max_concurrency=2)
    assert ac.max_concurrency == 2

    await ac.set_max_concurrency(4)
    assert ac.max_concurrency == 4

    # Should now be able to acquire 4
    for _ in range(4):
        result = await ac.acquire(timeout=0.1)
        assert result is True
    assert ac.active == 4

    result = await ac.acquire(timeout=0.1)
    assert result is False

    for _ in range(4):
        await ac.release()


@pytest.mark.asyncio
async def test_dynamic_concurrency_decrease():
    ac = AdmissionController(max_concurrency=4)
    await ac.set_max_concurrency(2)
    assert ac.max_concurrency == 2


@pytest.mark.asyncio
async def test_stats():
    ac = AdmissionController(max_concurrency=5)
    await ac.acquire()
    stats = ac.stats
    assert stats["max_concurrency"] == 5
    assert stats["active"] == 1
    assert stats["available"] == 4
    assert stats["total_admitted"] == 1
    await ac.release()


@pytest.mark.asyncio
async def test_concurrent_acquire():
    """Multiple tasks competing for slots."""
    ac = AdmissionController(max_concurrency=3)
    acquired = []

    async def worker(i: int):
        result = await ac.acquire(timeout=2.0)
        if result:
            acquired.append(i)
            await asyncio.sleep(0.1)
            await ac.release()

    tasks = [asyncio.create_task(worker(i)) for i in range(6)]
    await asyncio.gather(*tasks)

    # All 6 should eventually acquire (3 at a time)
    assert len(acquired) == 6
