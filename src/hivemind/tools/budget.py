"""hm.budget — Set and check token budgets."""

from __future__ import annotations

from ..scheduler.budget import BudgetManager


async def manage_budget(
    budget_manager: BudgetManager,
    action: str = "status",
    agent_id: str | None = None,
    budget: int | None = None,
    total_budget: int | None = None,
) -> dict:
    """Manage token budgets for agents.

    Args:
        action: "status", "set_agent", "set_total", or "reset"
        agent_id: Agent ID (required for set_agent)
        budget: Token budget to set
        total_budget: Global token budget

    Returns:
        Budget status or confirmation
    """
    if action == "status":
        stats = budget_manager.stats
        if agent_id:
            agent_budget = await budget_manager.get_agent_budget(agent_id)
            if agent_budget:
                stats["agent"] = {
                    "id": agent_id,
                    "budget": agent_budget.budget,
                    "used": agent_budget.used,
                    "remaining": agent_budget.remaining,
                }
            else:
                stats["agent"] = {"id": agent_id, "error": "not found"}
        return stats

    elif action == "set_agent":
        if not agent_id:
            return {"error": "agent_id required for set_agent"}
        if budget is None:
            return {"error": "budget required for set_agent"}
        ab = await budget_manager.set_agent_budget(agent_id, budget)
        return {
            "agent_id": agent_id,
            "budget": ab.budget,
            "used": ab.used,
            "remaining": ab.remaining,
            "message": f"Budget for agent {agent_id} set to {budget} tokens",
        }

    elif action == "set_total":
        if total_budget is None:
            return {"error": "total_budget required for set_total"}
        await budget_manager.set_total_budget(total_budget)
        return {
            "total_budget": total_budget,
            "total_used": budget_manager.total_used,
            "total_remaining": budget_manager.total_remaining,
            "message": f"Global token budget set to {total_budget}",
        }

    else:
        return {"error": f"Unknown action '{action}'. Use: status, set_agent, set_total"}
