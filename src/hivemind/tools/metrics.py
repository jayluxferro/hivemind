"""hm.metrics — Scheduler performance stats."""

from __future__ import annotations

from ..proxy.latency_tracker import LatencyTracker
from ..scheduler.admission import AdmissionController
from ..scheduler.backpressure import BackpressureController
from ..scheduler.budget import BudgetManager
from ..scheduler.queue import PriorityQueue
from ..scheduler.rate_limiter import RateLimiter
from ..storage.db import Database


async def get_metrics(
    admission: AdmissionController,
    rate_limiter: RateLimiter,
    backpressure: BackpressureController,
    budget_manager: BudgetManager,
    queue: PriorityQueue,
    latency_tracker: LatencyTracker,
    db: Database | None = None,
) -> dict:
    """Get comprehensive scheduler performance metrics.

    Returns:
        Dict with metrics from all scheduler components
    """
    result = {
        "admission": admission.stats,
        "rate_limiter": rate_limiter.stats,
        "backpressure": backpressure.stats,
        "budget": budget_manager.stats,
        "queue": queue.stats,
        "latency": latency_tracker.stats,
    }

    if db:
        try:
            result["aggregate"] = await db.get_aggregate_stats()
        except Exception:
            pass

    return result
