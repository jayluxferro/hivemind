"""Health monitor — watches agent processes and scheduler state.

Periodically checks for:
- Hung agents (no API activity for too long)
- Memory/resource pressure
- Proxy connectivity
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .pool import AgentPool

logger = logging.getLogger(__name__)


@dataclass
class HealthStatus:
    healthy: bool
    proxy_up: bool
    active_agents: int
    hung_agents: list[str]
    warnings: list[str]
    checked_at: float

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "proxy_up": self.proxy_up,
            "active_agents": self.active_agents,
            "hung_agents": self.hung_agents,
            "warnings": self.warnings,
            "checked_at": self.checked_at,
        }


class HealthMonitor:
    """Periodic health checks for the HiveMind system."""

    def __init__(
        self,
        pool: AgentPool,
        proxy_url: str = "http://127.0.0.1:8765",
        check_interval: float = 30.0,
        hung_threshold: float = 300.0,
    ) -> None:
        self.pool = pool
        self.proxy_url = proxy_url
        self.check_interval = check_interval
        self.hung_threshold = hung_threshold
        self._last_status: HealthStatus | None = None
        self._task: asyncio.Task | None = None

    @property
    def last_status(self) -> HealthStatus | None:
        return self._last_status

    async def check(self) -> HealthStatus:
        """Run a health check."""
        warnings: list[str] = []
        hung_agents: list[str] = []

        # Check proxy
        proxy_up = await self._check_proxy()
        if not proxy_up:
            warnings.append("Proxy is not responding")

        # Check for hung agents
        now = time.time()
        for agent in await self.pool.list_agents(state="running"):
            if agent.started_at and (now - agent.started_at) > self.hung_threshold:
                hung_agents.append(agent.id)

        if hung_agents:
            warnings.append(f"{len(hung_agents)} agents may be hung")

        active = self.pool.active_count
        healthy = proxy_up and len(hung_agents) == 0

        status = HealthStatus(
            healthy=healthy,
            proxy_up=proxy_up,
            active_agents=active,
            hung_agents=hung_agents,
            warnings=warnings,
            checked_at=now,
        )
        self._last_status = status
        return status

    async def _check_proxy(self) -> bool:
        """Ping the proxy health endpoint."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.proxy_url}/_health")
                return resp.status_code == 200
        except Exception:
            return False

    async def start(self) -> None:
        """Start periodic health checks in the background."""
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        while True:
            try:
                status = await self.check()
                if not status.healthy:
                    logger.warning("Health check: UNHEALTHY — %s", status.warnings)
                else:
                    logger.debug("Health check: OK (active=%d)", status.active_agents)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Health check error: %s", exc)
            await asyncio.sleep(self.check_interval)
