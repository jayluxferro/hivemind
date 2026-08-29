"""Mock agent — makes a series of API calls to simulate an LLM coding agent.

Each mock agent:
1. Sends N requests to the API (simulating multi-turn conversation)
2. Records success/failure for each request
3. Reports total tokens used and wall time
4. Can be killed via budget exhaustion or external signal

Used by the harness to simulate real agent workloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Results from a single agent run."""

    agent_id: str
    requests_made: int = 0
    requests_succeeded: int = 0
    requests_failed: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0
    completed_at: float = 0.0
    alive: bool = True  # False if the agent "died" (unrecoverable error)

    @property
    def wall_time_s(self) -> float:
        return self.completed_at - self.started_at

    @property
    def failure_rate(self) -> float:
        if self.requests_made == 0:
            return 0.0
        return self.requests_failed / self.requests_made

    @property
    def total_tokens(self) -> int:
        return self.total_tokens_in + self.total_tokens_out

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "alive": self.alive,
            "requests_made": self.requests_made,
            "requests_succeeded": self.requests_succeeded,
            "requests_failed": self.requests_failed,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_tokens": self.total_tokens,
            "wall_time_s": round(self.wall_time_s, 2),
            "failure_rate": round(self.failure_rate, 3),
            "errors": self.errors[-5:],  # Last 5 errors
        }


@dataclass
class AgentConfig:
    """Configuration for a mock agent's behavior."""

    turns: int = 10  # Number of API call rounds
    message_length: int = 200  # Characters per message
    model: str = "claude-sonnet-4-20250514"
    max_retries: int = 0  # 0 = no retry (agent dies on error) — this is "without HiveMind"
    inter_turn_delay_ms: float = 50.0  # Delay between turns (simulates agent thinking)
    die_on_error: bool = True  # If True, agent stops on first unrecoverable error


async def run_mock_agent(
    agent_id: str,
    base_url: str,
    config: AgentConfig | None = None,
    api_key: str = "mock-key",
) -> AgentResult:
    """Run a single mock agent that makes API calls.

    Args:
        agent_id: Unique identifier for this agent
        base_url: API base URL (mock API or HiveMind proxy)
        config: Agent behavior configuration
        api_key: API key to send in headers
    """
    config = config or AgentConfig()
    result = AgentResult(agent_id=agent_id, started_at=time.time())

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
    ) as client:
        for turn in range(config.turns):
            result.requests_made += 1

            request_body = {
                "model": config.model,
                "max_tokens": 1024,
                "messages": [
                    {
                        "role": "user",
                        "content": _generate_message(config.message_length, turn),
                    }
                ],
            }

            success = False
            retries = 0
            while retries <= config.max_retries:
                try:
                    response = await client.post(
                        f"{base_url}/v1/messages",
                        json=request_body,
                        headers={
                            "x-api-key": api_key,
                            "content-type": "application/json",
                            "anthropic-version": "2023-06-01",
                            "x-hivemind-agent-id": agent_id,
                        },
                    )

                    if response.status_code == 200:
                        data = response.json()
                        usage = data.get("usage", {})
                        result.total_tokens_in += usage.get("input_tokens", 0)
                        result.total_tokens_out += usage.get("output_tokens", 0)
                        result.requests_succeeded += 1
                        success = True
                        break
                    elif response.status_code == 429:
                        # Rate limited
                        if retries < config.max_retries:
                            retry_after = float(response.headers.get("retry-after", "2"))
                            await asyncio.sleep(retry_after)
                            retries += 1
                            continue
                        result.errors.append(f"turn {turn}: 429 rate limited (retries exhausted)")
                        break
                    elif response.status_code in (502, 529):
                        if retries < config.max_retries:
                            await asyncio.sleep(1.0 * (2**retries))
                            retries += 1
                            continue
                        result.errors.append(f"turn {turn}: {response.status_code}")
                        break
                    else:
                        result.errors.append(f"turn {turn}: HTTP {response.status_code}")
                        break

                except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.PoolTimeout) as exc:
                    if retries < config.max_retries:
                        await asyncio.sleep(1.0 * (2**retries))
                        retries += 1
                        continue
                    result.errors.append(f"turn {turn}: {type(exc).__name__}: {exc}")
                    break

            if not success:
                result.requests_failed += 1
                if config.die_on_error:
                    result.alive = False
                    break

            # Inter-turn delay
            if turn < config.turns - 1:
                delay = config.inter_turn_delay_ms / 1000.0
                if delay > 0:
                    await asyncio.sleep(delay)

    result.completed_at = time.time()
    return result


def _generate_message(length: int, turn: int) -> str:
    """Generate a realistic-ish message for the mock agent."""
    prompts = [
        "Write a function that",
        "Fix the bug in",
        "Refactor the code to",
        "Add tests for",
        "Explain how",
        "Create a class that",
        "Optimize the performance of",
        "Debug the issue with",
        "Implement the feature for",
        "Review and improve",
    ]
    base = prompts[turn % len(prompts)]
    padding = " detailed implementation" * (length // 25)
    return (base + padding)[:length]
