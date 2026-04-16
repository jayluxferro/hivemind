"""Tests for the token budget manager."""

import pytest

from hivemind.scheduler.budget import BudgetExhausted, BudgetManager


@pytest.mark.asyncio
async def test_register_agent():
    bm = BudgetManager()
    ab = await bm.register_agent("agent-1", budget=10000)
    assert ab.agent_id == "agent-1"
    assert ab.budget == 10000
    assert ab.used == 0
    assert ab.remaining == 10000


@pytest.mark.asyncio
async def test_record_usage():
    bm = BudgetManager()
    await bm.register_agent("agent-1", budget=10000)

    ab = await bm.record_usage("agent-1", tokens_in=500, tokens_out=200)
    assert ab.used == 700
    assert ab.remaining == 9300


@pytest.mark.asyncio
async def test_budget_exhausted():
    bm = BudgetManager()
    await bm.register_agent("agent-1", budget=1000)

    with pytest.raises(BudgetExhausted):
        await bm.record_usage("agent-1", tokens_in=800, tokens_out=300)


@pytest.mark.asyncio
async def test_global_budget():
    bm = BudgetManager(total_budget=5000)
    await bm.register_agent("agent-1")
    await bm.register_agent("agent-2")

    await bm.record_usage("agent-1", tokens_in=2000, tokens_out=500)
    await bm.record_usage("agent-2", tokens_in=1000, tokens_out=500)

    assert bm.total_used == 4000
    assert bm.total_remaining == 1000


@pytest.mark.asyncio
async def test_default_agent_budget():
    bm = BudgetManager(default_agent_budget=5000)
    ab = await bm.register_agent("agent-1")
    assert ab.budget == 5000


@pytest.mark.asyncio
async def test_set_agent_budget():
    bm = BudgetManager()
    await bm.register_agent("agent-1", budget=1000)
    ab = await bm.set_agent_budget("agent-1", 5000)
    assert ab.budget == 5000


@pytest.mark.asyncio
async def test_auto_register_on_usage():
    bm = BudgetManager()
    # Recording usage for unknown agent should auto-register
    ab = await bm.record_usage("new-agent", tokens_in=100, tokens_out=50)
    assert ab.used == 150


@pytest.mark.asyncio
async def test_unlimited_budget():
    bm = BudgetManager()
    await bm.register_agent("agent-1")  # No budget set

    # Should not raise
    ab = await bm.record_usage("agent-1", tokens_in=100000, tokens_out=50000)
    assert ab.used == 150000
    assert ab.remaining is None


@pytest.mark.asyncio
async def test_stats():
    bm = BudgetManager(total_budget=100000)
    await bm.register_agent("a1", budget=50000)
    await bm.record_usage("a1", tokens_in=10000, tokens_out=5000)

    stats = bm.stats
    assert stats["total_budget"] == 100000
    assert stats["total_used"] == 15000
    assert "a1" in stats["agents"]
    assert stats["agents"]["a1"]["used"] == 15000
