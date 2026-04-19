"""Tests for the AIMD backpressure controller."""

import pytest

from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController


@pytest.mark.asyncio
async def test_set_concurrency_limits():
    bp = BackpressureController(max_concurrency=10, min_concurrency=1)
    bp._current_concurrency = 8.0
    await bp.set_concurrency_limits(3, 1)
    assert bp._max_concurrency == 3
    assert bp._min == 1
    assert bp._current_concurrency == 3.0


@pytest.mark.asyncio
async def test_set_aimd_params():
    bp = BackpressureController(max_concurrency=5, additive_increase=0.5, multiplicative_decrease=0.5)
    await bp.set_aimd_params(2.0, 0.25)
    assert bp._ai == 2.0
    assert bp._md == 0.25


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
    assert "circuit_open" in stats
    assert stats["circuit_open"] is False
    assert stats["window_size"] == 1


# --- Circuit breaker tests ---


@pytest.mark.asyncio
async def test_circuit_opens_on_high_error_rate():
    """Circuit trips when error rate >= 50% over the window."""
    bp = BackpressureController(max_concurrency=10, multiplicative_decrease=0.9)
    assert bp.circuit_open is False

    # Pump enough requests to fill the circuit window (default 20).
    # 10 successes + 10 errors = 50% error rate → should trip.
    for _ in range(10):
        await bp.record_success()
    for _ in range(10):
        await bp.record_error()

    assert bp.circuit_open is True


@pytest.mark.asyncio
async def test_circuit_stays_closed_below_threshold():
    """Circuit stays closed when error rate is below 50%."""
    bp = BackpressureController(max_concurrency=10, multiplicative_decrease=0.9)

    # 15 successes + 5 errors = 25% error rate → should NOT trip.
    for _ in range(15):
        await bp.record_success()
    for _ in range(5):
        await bp.record_error()

    assert bp.circuit_open is False


@pytest.mark.asyncio
async def test_circuit_half_open_after_cooldown():
    """After cooldown, circuit_open returns False (half-open) to allow a probe."""
    bp = BackpressureController(max_concurrency=10, multiplicative_decrease=0.9)
    # Use a tiny cooldown for testing
    bp._circuit_cooldown = 0.01

    # Trip the circuit
    for _ in range(10):
        await bp.record_success()
    for _ in range(10):
        await bp.record_error()
    assert bp.circuit_open is True

    # Wait past cooldown
    import asyncio
    await asyncio.sleep(0.02)

    # Should be half-open (circuit_open returns False)
    assert bp.circuit_open is False
    # Internal flag is still True until a probe succeeds
    assert bp._circuit_open is True


@pytest.mark.asyncio
async def test_circuit_closes_after_successful_probe():
    """A successful request during half-open state closes the circuit."""
    bp = BackpressureController(max_concurrency=10, multiplicative_decrease=0.9)
    bp._circuit_cooldown = 0.01

    # Trip the circuit
    for _ in range(10):
        await bp.record_success()
    for _ in range(10):
        await bp.record_error()
    assert bp.circuit_open is True

    # Wait past cooldown → half-open
    import asyncio
    await asyncio.sleep(0.02)
    assert bp.circuit_open is False

    # Successful probe closes the circuit
    await bp.record_success()
    assert bp._circuit_open is False
    assert bp.circuit_open is False


# --- Backpressure → Admission integration tests ---


@pytest.mark.asyncio
async def test_error_pushes_concurrency_to_admission():
    """record_error() should reduce the admission controller's max concurrency."""
    admission = AdmissionController(max_concurrency=10)
    bp = BackpressureController(
        max_concurrency=10,
        multiplicative_decrease=0.5,
        admission=admission,
    )

    assert admission.max_concurrency == 10
    await bp.record_error()
    # 10 * 0.5 = 5
    assert admission.max_concurrency == 5


@pytest.mark.asyncio
async def test_aimd_increase_pushes_to_admission():
    """AIMD additive increase should raise admission concurrency."""
    admission = AdmissionController(max_concurrency=10)
    bp = BackpressureController(
        max_concurrency=10,
        additive_increase=2.0,
        latency_target_ms=5000.0,
        update_interval=0.0,
        admission=admission,
    )
    bp._current_concurrency = 5.0
    # Push current concurrency down in admission to match
    await admission.set_max_concurrency(5)

    # Record low latency → should increase
    for _ in range(5):
        await bp.record_latency(100.0)

    assert admission.max_concurrency > 5


@pytest.mark.asyncio
async def test_set_admission_deferred_wiring():
    """set_admission() wires the controller after construction."""
    admission = AdmissionController(max_concurrency=10)
    bp = BackpressureController(max_concurrency=10, multiplicative_decrease=0.5)

    # Not wired yet — error should not crash
    await bp.record_error()
    assert admission.max_concurrency == 10  # unchanged

    # Wire it
    bp.set_admission(admission)
    await bp.record_error()
    assert admission.max_concurrency < 10  # now applied
