"""Tests for the latency tracker."""

from hivemind.proxy.latency_tracker import LatencyTracker


def test_empty_tracker():
    lt = LatencyTracker()
    assert lt.avg_ms == 0.0
    assert lt.p50_ms == 0.0
    assert lt.p95_ms == 0.0
    assert lt.error_rate == 0.0


def test_record_samples():
    lt = LatencyTracker()
    lt.record(100.0, status_code=200)
    lt.record(200.0, status_code=200)
    lt.record(300.0, status_code=200)

    assert lt.avg_ms == 200.0
    assert lt.min_ms == 100.0
    assert lt.max_ms == 300.0


def test_percentiles():
    lt = LatencyTracker()
    for i in range(100):
        lt.record(float(i + 1), status_code=200)

    assert 50.0 <= lt.p50_ms <= 52.0
    assert 94.0 <= lt.p95_ms <= 96.0
    assert 98.0 <= lt.p99_ms <= 100.0


def test_error_rate():
    lt = LatencyTracker()
    lt.record(100.0, status_code=200)
    lt.record(100.0, status_code=200)
    lt.record(100.0, status_code=429)
    lt.record(100.0, status_code=502)

    assert lt.error_rate == 0.5  # 2 errors out of 4


def test_rolling_window():
    lt = LatencyTracker(window_size=3)
    lt.record(100.0)
    lt.record(200.0)
    lt.record(300.0)
    lt.record(400.0)  # Should push out 100.0

    assert lt.avg_ms == 300.0  # (200 + 300 + 400) / 3
    assert lt.min_ms == 200.0


def test_stats():
    lt = LatencyTracker()
    lt.record(150.0, status_code=200)
    stats = lt.stats
    assert stats["total_requests"] == 1
    assert stats["avg_ms"] == 150.0
    assert "p50_ms" in stats
    assert "p95_ms" in stats
    assert "error_rate" in stats
