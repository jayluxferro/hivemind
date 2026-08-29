"""Real-world validation — runs against actual LLM API providers.

Designed to be cost-efficient:
- Uses cheapest available model (haiku / gpt-4o-mini)
- Short messages (~50 tokens input)
- Few turns per agent (3-5)
- But enough concurrent agents to trigger real rate limits

Measures the same metrics as mock evaluation:
- Failure rate (agents that die vs complete)
- Token waste (tokens consumed by dead agents)
- Wall time
- Actual rate limit encounters (429s)
- Actual connection errors

Usage:
    python -m evaluation.real_world --provider openai --agents 10
    python -m evaluation.real_world --provider anthropic --agents 10
    python -m evaluation.real_world --provider ollama --agents 10 --compare
    python -m evaluation.real_world --provider ollama --agents 10 --with-hivemind
    python -m evaluation.real_world --provider generic --base-url http://127.0.0.1:11435/v1 --model my-model --agents 10
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class RealWorldConfig:
    """Configuration for a real-world validation run."""

    # Provider
    provider: str = "openai"  # openai, anthropic, ollama, generic
    api_key: str = ""
    base_url: str = ""
    model: str = ""

    # Workload
    num_agents: int = 10
    turns_per_agent: int = 4
    message: str = "Reply with exactly one word: hello."

    # HiveMind
    use_hivemind: bool = False
    hm_max_concurrency: int = 5
    hm_proxy_port: int = 0  # 0 = auto

    def resolve(self) -> None:
        """Fill in defaults based on provider."""
        if self.provider == "openai":
            self.api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
            self.base_url = self.base_url or "https://api.openai.com/v1"
            self.model = self.model or "gpt-4o-mini"
        elif self.provider == "anthropic":
            self.api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            self.base_url = self.base_url or "https://api.anthropic.com"
            self.model = self.model or "claude-haiku-4-5-20251001"
        elif self.provider == "ollama":
            self.api_key = self.api_key or "ollama"  # Ollama doesn't need a real key
            self.base_url = self.base_url or "http://localhost:11434/v1"
            self.model = self.model or "qwen3.5:4b"
        elif self.provider == "generic":
            # Generic OpenAI-compatible endpoint (e.g. MLX, vLLM, llama.cpp)
            self.api_key = self.api_key or "none"
            if not self.base_url:
                raise ValueError("--base-url is required for generic provider")
            if not self.model:
                raise ValueError("--model is required for generic provider")
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

        if self.provider in ("openai", "anthropic") and not self.api_key:
            raise ValueError(f"No API key found for {self.provider}. Set {self.provider.upper()}_API_KEY")


@dataclass
class AgentOutcome:
    agent_id: str
    alive: bool = True
    turns_completed: int = 0
    turns_attempted: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    errors: list[str] = field(default_factory=list)
    status_codes: list[int] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def avg_latency_ms(self) -> float:
        return sum(self.latencies_ms) / len(self.latencies_ms) if self.latencies_ms else 0.0

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "alive": self.alive,
            "turns_completed": self.turns_completed,
            "turns_attempted": self.turns_attempted,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens,
            "errors": self.errors[-5:],
            "status_codes": self.status_codes,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "wall_time_s": round(self.completed_at - self.started_at, 2),
        }


@dataclass
class ValidationResult:
    config: RealWorldConfig
    outcomes: list[AgentOutcome] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0

    @property
    def agents_alive(self) -> int:
        return sum(1 for o in self.outcomes if o.alive)

    @property
    def agents_dead(self) -> int:
        return sum(1 for o in self.outcomes if not o.alive)

    @property
    def failure_rate(self) -> float:
        return self.agents_dead / len(self.outcomes) if self.outcomes else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(o.total_tokens for o in self.outcomes)

    @property
    def wasted_tokens(self) -> int:
        return sum(o.total_tokens for o in self.outcomes if not o.alive)

    @property
    def total_429s(self) -> int:
        return sum(o.status_codes.count(429) for o in self.outcomes)

    @property
    def total_5xx(self) -> int:
        return sum(1 for o in self.outcomes for s in o.status_codes if 500 <= s < 600)

    @property
    def wall_time_s(self) -> float:
        return self.completed_at - self.started_at

    def summary(self) -> dict:
        return {
            "provider": self.config.provider,
            "model": self.config.model,
            "mode": "hivemind" if self.config.use_hivemind else "direct",
            "num_agents": len(self.outcomes),
            "agents_alive": self.agents_alive,
            "agents_dead": self.agents_dead,
            "failure_rate_pct": round(self.failure_rate * 100, 1),
            "total_tokens": self.total_tokens,
            "wasted_tokens": self.wasted_tokens,
            "total_429s": self.total_429s,
            "total_5xx": self.total_5xx,
            "wall_time_s": round(self.wall_time_s, 2),
            "turns_per_agent": self.config.turns_per_agent,
        }

    def format_table(self) -> str:
        s = self.summary()
        lines = [
            f"{'=' * 60}",
            f"  Real-World Validation: {s['provider'].upper()} ({s['model']})",
            f"  Mode: {s['mode']}",
            f"{'=' * 60}",
            f"  Agents:        {s['num_agents']}",
            f"  Alive:         {s['agents_alive']}",
            f"  Dead:          {s['agents_dead']}",
            f"  Failure Rate:  {s['failure_rate_pct']}%",
            f"  Total Tokens:  {s['total_tokens']}",
            f"  Wasted Tokens: {s['wasted_tokens']}",
            f"  429 Errors:    {s['total_429s']}",
            f"  5xx Errors:    {s['total_5xx']}",
            f"  Wall Time:     {s['wall_time_s']}s",
            f"{'=' * 60}",
        ]
        return "\n".join(lines)


async def _run_agent_openai(
    agent_id: str,
    base_url: str,
    api_key: str,
    model: str,
    turns: int,
    message: str,
) -> AgentOutcome:
    """Run a single agent against OpenAI-compatible API."""
    outcome = AgentOutcome(agent_id=agent_id, started_at=time.time())
    messages = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10), verify=False) as client:
        for turn in range(turns):
            outcome.turns_attempted += 1
            messages.append({"role": "user", "content": message})

            start = time.monotonic()
            try:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json={"model": model, "max_tokens": 50, "messages": messages},
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
                latency = (time.monotonic() - start) * 1000
                outcome.latencies_ms.append(latency)
                outcome.status_codes.append(resp.status_code)

                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage", {})
                    outcome.tokens_in += usage.get("prompt_tokens", 0)
                    outcome.tokens_out += usage.get("completion_tokens", 0)
                    outcome.turns_completed += 1
                    reply = data["choices"][0]["message"]["content"]
                    messages.append({"role": "assistant", "content": reply})
                else:
                    outcome.errors.append(f"turn {turn}: HTTP {resp.status_code}")
                    outcome.alive = False
                    break

            except Exception as exc:
                outcome.errors.append(f"turn {turn}: {type(exc).__name__}: {exc}")
                outcome.alive = False
                break

    outcome.completed_at = time.time()
    return outcome


async def _run_agent_anthropic(
    agent_id: str,
    base_url: str,
    api_key: str,
    model: str,
    turns: int,
    message: str,
) -> AgentOutcome:
    """Run a single agent against Anthropic API."""
    outcome = AgentOutcome(agent_id=agent_id, started_at=time.time())
    messages = []

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10), verify=False) as client:
        for turn in range(turns):
            outcome.turns_attempted += 1
            messages.append({"role": "user", "content": message})

            start = time.monotonic()
            try:
                resp = await client.post(
                    f"{base_url}/v1/messages",
                    json={"model": model, "max_tokens": 50, "messages": messages},
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "Content-Type": "application/json",
                    },
                )
                latency = (time.monotonic() - start) * 1000
                outcome.latencies_ms.append(latency)
                outcome.status_codes.append(resp.status_code)

                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage", {})
                    outcome.tokens_in += usage.get("input_tokens", 0)
                    outcome.tokens_out += usage.get("output_tokens", 0)
                    outcome.turns_completed += 1
                    reply = data["content"][0]["text"]
                    messages.append({"role": "assistant", "content": reply})
                else:
                    outcome.errors.append(f"turn {turn}: HTTP {resp.status_code}")
                    outcome.alive = False
                    break

            except Exception as exc:
                outcome.errors.append(f"turn {turn}: {type(exc).__name__}: {exc}")
                outcome.alive = False
                break

    outcome.completed_at = time.time()
    return outcome


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run_validation(config: RealWorldConfig) -> ValidationResult:
    """Run a full real-world validation."""
    config.resolve()

    proxy_task = None
    agent_base_url = config.base_url

    # Start HiveMind proxy if requested
    if config.use_hivemind:
        from hivemind.proxy.server import ProxyServer
        from hivemind.scheduler.admission import AdmissionController
        from hivemind.scheduler.backpressure import BackpressureController
        from hivemind.scheduler.budget import BudgetManager
        from hivemind.scheduler.rate_limiter import RateLimiter
        from hivemind.storage.models import HiveMindConfig

        port = config.hm_proxy_port or _free_port()
        hm_config = HiveMindConfig(
            proxy_host="127.0.0.1",
            proxy_port=port,
            upstream_url=config.base_url,
            max_concurrency=config.hm_max_concurrency,
        )
        hm_config.apply_provider_defaults()

        proxy = ProxyServer(
            config=hm_config,
            admission=AdmissionController(hm_config.max_concurrency),
            rate_limiter=RateLimiter(),
            backpressure=BackpressureController(
                hm_config.max_concurrency, latency_target_ms=hm_config.latency_target_ms
            ),
            budget_manager=BudgetManager(),
        )
        proxy_task = asyncio.create_task(proxy.serve())

        # Wait for proxy
        async with httpx.AsyncClient() as c:
            for _ in range(50):
                try:
                    r = await c.get(f"http://127.0.0.1:{port}/_health", timeout=2)
                    if r.status_code == 200:
                        break
                except (httpx.ConnectError, httpx.ReadError):
                    pass
                await asyncio.sleep(0.1)

        agent_base_url = f"http://127.0.0.1:{port}"
        # Don't append /v1 for OpenAI — the agent path already includes it.
        # The proxy concatenates upstream_url + request_path, so adding /v1
        # here would produce /v1/v1/chat/completions.
        logger.info("HiveMind proxy at %s → %s", agent_base_url, config.base_url)

    # Select agent runner — ollama and generic use OpenAI-compatible format
    if config.provider == "anthropic":
        runner = _run_agent_anthropic
    else:
        runner = _run_agent_openai
    url = agent_base_url

    result = ValidationResult(config=config, started_at=time.time())

    logger.info(
        "Starting real-world validation: %s %s, %d agents x %d turns, mode=%s",
        config.provider,
        config.model,
        config.num_agents,
        config.turns_per_agent,
        "hivemind" if config.use_hivemind else "direct",
    )

    # Launch all agents simultaneously
    tasks = [
        runner(
            agent_id=f"agent-{i:03d}",
            base_url=url,
            api_key=config.api_key,
            model=config.model,
            turns=config.turns_per_agent,
            message=config.message,
        )
        for i in range(config.num_agents)
    ]

    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    for o in outcomes:
        if isinstance(o, Exception):
            result.outcomes.append(
                AgentOutcome(
                    agent_id="error",
                    alive=False,
                    errors=[str(o)],
                    started_at=result.started_at,
                    completed_at=time.time(),
                )
            )
        else:
            result.outcomes.append(o)

    result.completed_at = time.time()

    # Cleanup proxy
    if proxy_task:
        proxy_task.cancel()
        try:
            await proxy_task
        except (asyncio.CancelledError, SystemExit):
            pass

    return result


async def run_comparison(config: RealWorldConfig) -> tuple[ValidationResult, ValidationResult]:
    """Run both direct and HiveMind modes and return both results."""
    import copy

    # Direct — use a copy so the original config isn't mutated
    direct_config = copy.copy(config)
    direct_config.use_hivemind = False
    direct = await run_validation(direct_config)

    # Brief pause
    await asyncio.sleep(2.0)

    # HiveMind
    hm_config = copy.copy(config)
    hm_config.use_hivemind = True
    hivemind = await run_validation(hm_config)

    return direct, hivemind


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HiveMind Real-World Validation")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic", "ollama", "generic"])
    parser.add_argument("--model", default="", help="Model to use (default: cheapest for provider)")
    parser.add_argument("--base-url", default="", help="Base URL for the API (required for generic)")
    parser.add_argument("--agents", type=int, default=10, help="Number of concurrent agents")
    parser.add_argument("--turns", type=int, default=4, help="Turns per agent")
    parser.add_argument("--max-concurrency", type=int, default=5, help="HiveMind max concurrency")
    parser.add_argument("--with-hivemind", action="store_true", help="Run through HiveMind proxy")
    parser.add_argument("--compare", action="store_true", help="Run both direct and HiveMind, compare")
    parser.add_argument("--output", type=str, help="Save JSON results")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config = RealWorldConfig(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        num_agents=args.agents,
        turns_per_agent=args.turns,
        use_hivemind=args.with_hivemind,
        hm_max_concurrency=args.max_concurrency,
    )

    if args.compare:
        direct, hivemind = asyncio.run(run_comparison(config))
        print(direct.format_table())
        print()
        print(hivemind.format_table())
        print()
        print("--- Comparison ---")
        d, h = direct.summary(), hivemind.summary()
        print(f"  Failure rate: {d['failure_rate_pct']}% → {h['failure_rate_pct']}%")
        print(f"  Wasted tokens: {d['wasted_tokens']} → {h['wasted_tokens']}")
        print(f"  429 errors: {d['total_429s']} → {h['total_429s']}")
        print(f"  Wall time: {d['wall_time_s']}s → {h['wall_time_s']}s")

        if args.output:
            data = {"direct": d, "hivemind": h}
            with open(args.output, "w") as f:
                json.dump(data, f, indent=2)
            print(f"\nResults saved to {args.output}")
    else:
        result = asyncio.run(run_validation(config))
        print(result.format_table())
        print()
        for o in result.outcomes:
            status = "ALIVE" if o.alive else "DEAD"
            print(
                f"  {o.agent_id}: {status} turns={o.turns_completed}/{o.turns_attempted} "
                f"tokens={o.total_tokens} latency={o.avg_latency_ms:.0f}ms "
                f"{' '.join(o.errors[-2:])}"
            )

        if args.output:
            data = result.summary()
            data["agents"] = [o.to_dict() for o in result.outcomes]
            with open(args.output, "w") as f:
                json.dump(data, f, indent=2)
            print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
