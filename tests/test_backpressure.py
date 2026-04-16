"""Tests for the AIMD backpressure controller."""

import pytest

from hivemind.scheduler.backpressure import BackpressureController


@pytest.mark.asyncio
async def test_initial_state():
    bp = BackpressureController(max_concurrency=5)
    assert bp.recommended_concurrency == 5
    assert bp.backpressure_factor == 0.0
    assert bp.current_avg_latency_ms == 0.0


@pytest.mark.asyncio
async def test_low_latency_increases_concurrency():
    bp = BackpressureController(
        max_concurrency=10,
        additive_increase=1.0,
        latency_target_ms=2000.0,
        update_interval=0.0,  # Immediate updates for testing
    )
    # Start at 10, record low latency
    # (current starts at max_concurrency=10, already at max)
    bp._current_concurrency = 5.0

    for _ in range(5):
        await bp.record_latency(500.0)  # Well under target

    assert bp._current_concurrency > 5.0


@pytest.mark.asyncio
async def test_high_latency_decreases_concurrency():
    bp = BackpressureController(
        max_concurrency=10,
        multiplicative_decrease=0.5,
        latency_target_ms=1000.0,
        update_interval=0.0,
    )
    bp._current_concurrency = 8.0

    for _ in range(5):
        await bp.record_latency(3000.0)  # Well over target

    assert bp._current_concurrency < 8.0


@pytest.mark.asyncio
async def test_error_reduces_concurrency():
    bp = BackpressureController(
        max_concurrency=10,
        multiplicative_decrease=0.5,
    )
    bp._current_concurrency = 8.0

    await bp.record_error()
    assert bp._current_concurrency == 4.0  # 8 * 0.5


@pytest.mark.asyncio
async def test_min_concurrency_floor():
    bp = BackpressureController(
        max_concurrency=10,
        multiplicative_decrease=0.5,
        min_concurrency=2,
    )
    bp._current_concurrency = 3.0

    await bp.record_error()
    # 3 * 0.5 = 1.5, but min is 2
    assert bp._current_concurrency == 2.0
    assert bp.recommended_concurrency == 2


@pytest.mark.asyncio
async def test_backpressure_factor():
    bp = BackpressureController(max_concurrency=10, min_concurrency=1)

    # At max → factor should be 0
    bp._current_concurrency = 10.0
    assert bp.backpressure_factor == 0.0

    # At min → factor should be 1
    bp._current_concurrency = 1.0
    assert bp.backpressure_factor == 1.0

    # In the middle
    bp._current_concurrency = 5.5
    assert 0.0 < bp.backpressure_factor < 1.0


@pytest.mark.asyncio
async def test_stats():
    bp = BackpressureController(max_concurrency=5)
    await bp.record_latency(100.0)
    stats = bp.stats
    assert "recommended_concurrency" in stats
    assert "avg_latency_ms" in stats
    assert "backpressure_factor" in stats
    assert stats["window_size"] == 1
