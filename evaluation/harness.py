"""Evaluation harness — runs benchmark scenarios with and without HiveMind.

Orchestrates:
1. Mock API server (configurable failure modes)
2. Optional HiveMind proxy
3. N concurrent mock agents
4. Metrics collection and comparison
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field

import httpx

from hivemind.proxy.server import ProxyServer
from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController
from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.storage.models import HiveMindConfig

from .mock_agent import AgentConfig, AgentResult, run_mock_agent
from .mock_api import MockAPIConfig, MockAPIServer

logger = logging.getLogger(__name__)


@dataclass
class ScenarioConfig:
    """A single benchmark scenario."""

    name: str
    description: str = ""

    # Agent configuration
    num_agents: int = 11
    agent_turns: int = 10
    agent_message_length: int = 200

    # Mock API configuration
    api_requests_per_minute: int = 50
    api_tokens_per_minute: int = 100_000
    api_error_rate: float = 0.0
    api_connection_reset_rate: float = 0.0
    api_base_latency_ms: float = 100.0
    api_latency_jitter_ms: float = 50.0
    api_latency_spike_rate: float = 0.0
    api_max_concurrent: int = 0

    # HiveMind configuration (when enabled)
    hm_max_concurrency: int = 5
    hm_total_token_budget: int | None = None
    hm_default_agent_budget: int | None = None

    # Ablation: disable specific primitives
    enable_admission: bool = True
    enable_rate_limiter: bool = True
    enable_backpressure: bool = True
    enable_budget: bool = True
    enable_retry: bool = True


@dataclass
class ScenarioResult:
    """Results from running a scenario."""

    scenario_name: str
    mode: str  # "direct" or "hivemind"
    agent_results: list[AgentResult] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0
    mock_api_stats: dict = field(default_factory=dict)
    hivemind_stats: dict = field(default_factory=dict)

    @property
    def wall_time_s(self) -> float:
        return self.completed_at - self.started_at

    @property
    def agents_alive(self) -> int:
        return sum(1 for r in self.agent_results if r.alive)

    @property
    def agents_dead(self) -> int:
        return sum(1 for r in self.agent_results if not r.alive)

    @property
    def failure_rate(self) -> float:
        n = len(self.agent_results)
        return self.agents_dead / n if n > 0 else 0.0

    @property
    def total_requests(self) -> int:
        return sum(r.requests_made for r in self.agent_results)

    @property
    def total_succeeded(self) -> int:
        return sum(r.requests_succeeded for r in self.agent_results)

    @property
    def total_failed(self) -> int:
        return sum(r.requests_failed for r in self.agent_results)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.agent_results)

    @property
    def wasted_tokens(self) -> int:
        """Tokens consumed by agents that died."""
        return sum(r.total_tokens for r in self.agent_results if not r.alive)

    @property
    def throughput_tasks_per_min(self) -> float:
        if self.wall_time_s == 0:
            return 0.0
        return (self.agents_alive / self.wall_time_s) * 60

    def summary(self) -> dict:
        return {
            "scenario": self.scenario_name,
            "mode": self.mode,
            "num_agents": len(self.agent_results),
            "agents_alive": self.agents_alive,
            "agents_dead": self.agents_dead,
            "failure_rate": round(self.failure_rate * 100, 1),
            "total_requests": self.total_requests,
            "total_succeeded": self.total_succeeded,
            "total_failed": self.total_failed,
            "total_tokens": self.total_tokens,
            "wasted_tokens": self.wasted_tokens,
            "wall_time_s": round(self.wall_time_s, 2),
            "throughput_tasks_per_min": round(self.throughput_tasks_per_min, 2),
        }


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BenchmarkHarness:
    """Runs benchmark scenarios with and without HiveMind."""

    def __init__(self) -> None:
        self._mock_api: MockAPIServer | None = None
        self._mock_api_task: asyncio.Task | None = None
        self._proxy: ProxyServer | None = None
        self._proxy_task: asyncio.Task | None = None
        self._api_port: int = 0
        self._proxy_port: int = 0

    async def run_scenario(
        self,
        scenario: ScenarioConfig,
        use_hivemind: bool = False,
    ) -> ScenarioResult:
        """Run a single scenario.

        Args:
            scenario: The scenario configuration
            use_hivemind: If True, route agents through HiveMind proxy.
                         If False, agents hit the mock API directly.
        """
        mode = "hivemind" if use_hivemind else "direct"
        logger.info("=== Running scenario '%s' [%s] with %d agents ===", scenario.name, mode, scenario.num_agents)

        # Allocate dynamic ports
        self._api_port = _find_free_port()

        # Start mock API
        api_config = MockAPIConfig(
            port=self._api_port,
            requests_per_minute=scenario.api_requests_per_minute,
            tokens_per_minute=scenario.api_tokens_per_minute,
            error_rate=scenario.api_error_rate,
            connection_reset_rate=scenario.api_connection_reset_rate,
            base_latency_ms=scenario.api_base_latency_ms,
            latency_jitter_ms=scenario.api_latency_jitter_ms,
            latency_spike_rate=scenario.api_latency_spike_rate,
            max_concurrent=scenario.api_max_concurrent,
        )
        self._mock_api = MockAPIServer(api_config)
        self._mock_api_task = asyncio.create_task(self._mock_api.serve())

        api_url = f"http://127.0.0.1:{self._api_port}"
        await self._wait_for_server(f"{api_url}/_health")

        # Determine agent target URL
        if use_hivemind:
            self._proxy_port = _find_free_port()
            await self._start_proxy(scenario)
            agent_base_url = f"http://127.0.0.1:{self._proxy_port}"
        else:
            agent_base_url = api_url

        # Configure agents
        agent_config = AgentConfig(
            turns=scenario.agent_turns,
            message_length=scenario.agent_message_length,
            max_retries=0,  # HiveMind proxy handles retries; agents die on first error
            die_on_error=True,
        )

        # Run all agents concurrently
        result = ScenarioResult(
            scenario_name=scenario.name,
            mode=mode,
            started_at=time.time(),
        )

        tasks = [
            run_mock_agent(
                agent_id=f"agent-{i:03d}",
                base_url=agent_base_url,
                config=agent_config,
            )
            for i in range(scenario.num_agents)
        ]

        agent_results = await asyncio.gather(*tasks, return_exceptions=True)

        for ar in agent_results:
            if isinstance(ar, Exception):
                logger.error("Agent exception: %s", ar)
                result.agent_results.append(
                    AgentResult(
                        agent_id="error",
                        alive=False,
                        started_at=result.started_at,
                        completed_at=time.time(),
                        errors=[str(ar)],
                    )
                )
            else:
                result.agent_results.append(ar)

        result.completed_at = time.time()

        # Collect stats
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{api_url}/_stats")
                result.mock_api_stats = resp.json()
        except Exception:
            pass

        if use_hivemind and self._proxy:
            result.hivemind_stats = self._proxy.get_stats()

        # Cleanup
        await self._stop_all()

        logger.info(
            "=== Scenario '%s' [%s] complete: %d/%d alive, %.1f%% failure rate, %.1fs wall time ===",
            scenario.name,
            mode,
            result.agents_alive,
            len(result.agent_results),
            result.failure_rate * 100,
            result.wall_time_s,
        )

        return result

    async def run_comparison(self, scenario: ScenarioConfig) -> tuple[ScenarioResult, ScenarioResult]:
        """Run a scenario both with and without HiveMind and return both results."""
        direct = await self.run_scenario(scenario, use_hivemind=False)
        await asyncio.sleep(0.5)
        hivemind = await self.run_scenario(scenario, use_hivemind=True)
        return direct, hivemind

    async def _start_proxy(self, scenario: ScenarioConfig) -> None:
        """Start HiveMind proxy with scenario-specific config."""
        config = HiveMindConfig(
            proxy_host="127.0.0.1",
            proxy_port=self._proxy_port,
            upstream_url=f"http://127.0.0.1:{self._api_port}",
            max_concurrency=scenario.hm_max_concurrency if scenario.enable_admission else 1000,
            max_retries=3 if scenario.enable_retry else 0,
            total_token_budget=scenario.hm_total_token_budget if scenario.enable_budget else None,
            default_agent_budget=scenario.hm_default_agent_budget if scenario.enable_budget else None,
        )

        admission = AdmissionController(config.max_concurrency)
        rate_limiter = RateLimiter()
        backpressure = BackpressureController(
            max_concurrency=config.max_concurrency,
            latency_target_ms=config.latency_target_ms if scenario.enable_backpressure else 999999.0,
        )
        budget_manager = BudgetManager(
            total_budget=config.total_token_budget,
            default_agent_budget=config.default_agent_budget,
        )

        self._proxy = ProxyServer(
            config=config,
            admission=admission,
            rate_limiter=rate_limiter,
            backpressure=backpressure,
            budget_manager=budget_manager,
        )
        self._proxy_task = asyncio.create_task(self._proxy.serve())
        await self._wait_for_server(f"http://127.0.0.1:{self._proxy_port}/_health")

    async def _wait_for_server(self, url: str, timeout: float = 10.0) -> None:
        """Wait for a server to become ready."""
        start = time.monotonic()
        async with httpx.AsyncClient() as client:
            while time.monotonic() - start < timeout:
                try:
                    resp = await client.get(url, timeout=2.0)
                    if resp.status_code == 200:
                        return
                except (httpx.ConnectError, httpx.ReadError):
                    pass
                await asyncio.sleep(0.1)
        raise TimeoutError(f"Server at {url} did not become ready within {timeout}s")

    async def _stop_all(self) -> None:
        """Stop all servers."""
        for task in [self._proxy_task, self._mock_api_task]:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, SystemExit):
                    pass
        self._proxy = None
        self._proxy_task = None
        self._mock_api = None
        self._mock_api_task = None
        await asyncio.sleep(0.3)
