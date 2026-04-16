"""Token budget manager — per-agent ceilings and global pool.

Tracks token usage across all agents and enforces limits:
- Global pool: total tokens available across all agents
- Per-agent ceiling: max tokens any single agent can consume
- Warning thresholds: notify when approaching limits
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class BudgetExhausted(Exception):
    """Raised when an agent has exceeded its token budget."""

    def __init__(self, agent_id: str, used: int, budget: int) -> None:
        self.agent_id = agent_id
        self.used = used
        self.budget = budget
        super().__init__(f"Agent {agent_id} exhausted budget: {used}/{budget} tokens")


@dataclass
class AgentBudget:
    agent_id: str
    budget: int | None = None  # None = unlimited
    used: int = 0
    warning_sent: bool = False
    created_at: float = field(default_factory=time.time)

    @property
    def remaining(self) -> int | None:
        if self.budget is None:
            return None
        return max(0, self.budget - self.used)

    @property
    def usage_ratio(self) -> float | None:
        if self.budget is None or self.budget == 0:
            return None
        return self.used / self.budget


class BudgetManager:
    """Manages token budgets at both global and per-agent levels."""

    def __init__(
        self,
        total_budget: int | None = None,
        default_agent_budget: int | None = None,
        warning_threshold: float = 0.85,
    ) -> None:
        self._total_budget = total_budget
        self._total_used = 0
        self._default_agent_budget = default_agent_budget
        self._warning_threshold = warning_threshold
        self._agents: dict[str, AgentBudget] = {}
        self._lock = asyncio.Lock()
        self._callbacks: list = []

    @property
    def total_budget(self) -> int | None:
        return self._total_budget

    @property
    def total_used(self) -> int:
        return self._total_used

    @property
    def total_remaining(self) -> int | None:
        if self._total_budget is None:
            return None
        return max(0, self._total_budget - self._total_used)

    def _register_agent_unlocked(self, agent_id: str, budget: int | None = None) -> AgentBudget:
        """Internal: register without acquiring lock (caller must hold lock)."""
        agent_budget = AgentBudget(
            agent_id=agent_id,
            budget=budget or self._default_agent_budget,
        )
        self._agents[agent_id] = agent_budget
        logger.info(
            "Budget: registered agent %s with budget=%s",
            agent_id,
            agent_budget.budget,
        )
        return agent_budget

    async def register_agent(self, agent_id: str, budget: int | None = None) -> AgentBudget:
        """Register an agent with an optional per-agent budget."""
        async with self._lock:
            return self._register_agent_unlocked(agent_id, budget)

    async def record_usage(self, agent_id: str, tokens_in: int, tokens_out: int) -> AgentBudget:
        """Record token usage for an agent. Returns updated budget. Raises BudgetExhausted."""
        total_tokens = tokens_in + tokens_out
        async with self._lock:
            if agent_id not in self._agents:
                self._register_agent_unlocked(agent_id)

            agent = self._agents[agent_id]
            agent.used += total_tokens
            self._total_used += total_tokens

            # Check per-agent budget
            if agent.budget is not None:
                if agent.used >= agent.budget:
                    logger.warning(
                        "Budget: agent %s EXHAUSTED (%d/%d tokens)",
                        agent_id,
                        agent.used,
                        agent.budget,
                    )
                    raise BudgetExhausted(agent_id, agent.used, agent.budget)

                ratio = agent.usage_ratio
                if ratio and ratio >= self._warning_threshold and not agent.warning_sent:
                    agent.warning_sent = True
                    logger.warning(
                        "Budget: agent %s at %.0f%% (%d/%d tokens)",
                        agent_id,
                        ratio * 100,
                        agent.used,
                        agent.budget,
                    )

            # Check global budget
            if self._total_budget is not None and self._total_used >= self._total_budget:
                logger.warning(
                    "Budget: GLOBAL budget exhausted (%d/%d tokens)",
                    self._total_used,
                    self._total_budget,
                )

            return agent

    async def get_agent_budget(self, agent_id: str) -> AgentBudget | None:
        return self._agents.get(agent_id)

    async def set_agent_budget(self, agent_id: str, budget: int) -> AgentBudget:
        async with self._lock:
            if agent_id not in self._agents:
                return self._register_agent_unlocked(agent_id, budget)
            self._agents[agent_id].budget = budget
            self._agents[agent_id].warning_sent = False
            return self._agents[agent_id]

    async def set_total_budget(self, budget: int | None) -> None:
        async with self._lock:
            self._total_budget = budget

    async def remove_agent(self, agent_id: str) -> None:
        async with self._lock:
            self._agents.pop(agent_id, None)

    @property
    def stats(self) -> dict:
        agents = {}
        for aid, ab in self._agents.items():
            agents[aid] = {
                "budget": ab.budget,
                "used": ab.used,
                "remaining": ab.remaining,
                "usage_ratio": round(ab.usage_ratio, 3) if ab.usage_ratio is not None else None,
            }
        return {
            "total_budget": self._total_budget,
            "total_used": self._total_used,
            "total_remaining": self.total_remaining,
            "default_agent_budget": self._default_agent_budget,
            "agents": agents,
        }
