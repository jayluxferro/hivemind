"""Data models for HiveMind scheduler."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

_DEFAULT_DB_URL = "postgresql://hivemind@localhost:5432/hivemind"


def _default_db_url() -> str:
    """Default Postgres DSN for config and ``--db`` options.

    ``HIVEMIND_DB_URL`` overrides the default; the ``or`` guards
    against an empty-string env value (lattice ``_db_default`` pattern).
    """
    dsn = os.environ.get("HIVEMIND_DB_URL", _DEFAULT_DB_URL)
    return dsn or _DEFAULT_DB_URL


class TaskState(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CHECKPOINTED = "checkpointed"


class TaskPriority(int, Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class Task:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    command: str = ""
    state: TaskState = TaskState.PENDING
    priority: TaskPriority = TaskPriority.NORMAL

    # Token budget
    token_budget: int | None = None
    tokens_used: int = 0

    # Scheduling
    dependencies: list[str] = field(default_factory=list)
    estimated_tokens: int | None = None

    # Timing (unix timestamps)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None

    # Metadata
    agent_id: str | None = None
    error: str | None = None
    result: str | None = None
    checkpoint_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # For priority queue comparison — higher priority first, then earlier creation
    def __lt__(self, other: Task) -> bool:
        if self.priority != other.priority:
            return self.priority.value > other.priority.value
        return self.created_at < other.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": self.command,
            "state": self.state.value,
            "priority": self.priority.value,
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "dependencies": self.dependencies,
            "estimated_tokens": self.estimated_tokens,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "agent_id": self.agent_id,
            "error": self.error,
            "result": self.result,
            "checkpoint_path": self.checkpoint_path,
            "metadata": self.metadata,
        }


@dataclass
class AgentMetrics:
    agent_id: str
    task_id: str
    tokens_in: int = 0
    tokens_out: int = 0
    requests_made: int = 0
    requests_failed: int = 0
    retries: int = 0
    avg_latency_ms: float = 0.0
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "requests_made": self.requests_made,
            "requests_failed": self.requests_failed,
            "retries": self.retries,
            "avg_latency_ms": self.avg_latency_ms,
            "recorded_at": self.recorded_at,
        }


@dataclass
class RateLimitSnapshot:
    provider: str
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    reset_at: float | None = None
    recorded_at: float = field(default_factory=time.time)


@dataclass
class SchedulerSnapshot:
    active_agents: int = 0
    max_concurrency: int = 5
    queued_tasks: int = 0
    total_tokens_used: int = 0
    total_token_budget: int | None = None
    current_latency_ms: float = 0.0
    backpressure_factor: float = 1.0
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_agents": self.active_agents,
            "max_concurrency": self.max_concurrency,
            "queued_tasks": self.queued_tasks,
            "total_tokens_used": self.total_tokens_used,
            "total_token_budget": self.total_token_budget,
            "current_latency_ms": self.current_latency_ms,
            "backpressure_factor": self.backpressure_factor,
            "recorded_at": self.recorded_at,
        }


@dataclass
class HiveMindConfig:
    """Runtime configuration for the HiveMind scheduler."""

    # Proxy
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8765
    upstream_url: str = "https://api.anthropic.com"

    # Admission control
    max_concurrency: int = 5

    # Token budgets
    total_token_budget: int | None = None
    default_agent_budget: int | None = None

    # Backpressure (AIMD)
    aimd_additive_increase: float = 0.5
    aimd_multiplicative_decrease: float = 0.5
    latency_target_ms: float = 2000.0
    min_concurrency: int = 1

    # Retry
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0

    # Rate limits (None = use provider profile defaults)
    rpm_limit: int | None = None
    tpm_limit: int | None = None
    # Sliding-window scope: "per_agent" buckets RPM/TPM by agent identity so
    # one session can't stall another; "global" shares one window (original
    # behavior). Header-driven throttling stays global either way.
    rate_limit_scope: str = "per_agent"
    # Per-agent rate-limit overrides: {agent_id: {"rpm": int, "tpm": int}}.
    # Agents missing a key fall back to rpm_limit/tpm_limit (then the provider
    # profile). Overrides are caps — the fair-share governor can still shrink
    # them under provider-key saturation.
    agent_limit_overrides: dict[str, dict[str, int]] = field(default_factory=dict)

    # Storage
    db_url: str = field(default_factory=_default_db_url)

    # Provider (auto-detected from upstream_url if not set)
    provider: str | None = None  # anthropic, openai, ollama, etc.

    # MCP
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8766

    # Upstream HTTP client (httpx verify=…)
    http_tls_verify: bool = True

    def __post_init__(self) -> None:
        self.normalize_runtime_limits()

    def apply_provider_defaults(self) -> None:
        """Auto-detect provider from upstream URL and apply its defaults.

        Only sets values that are still at their generic defaults,
        so explicit user config always wins.
        """
        from ..scheduler.providers import detect_provider

        profile = detect_provider(self.upstream_url)
        self.provider = profile.provider_type.value

        # Only override defaults — if the user explicitly set these, leave them
        if self.max_concurrency == 5:
            self.max_concurrency = profile.default_max_concurrent
        if self.latency_target_ms == 2000.0:
            self.latency_target_ms = profile.latency_target_ms
        if self.aimd_additive_increase == 0.5:
            self.aimd_additive_increase = profile.aimd_additive_increase
        if self.aimd_multiplicative_decrease == 0.5:
            self.aimd_multiplicative_decrease = profile.aimd_multiplicative_decrease

        self.normalize_runtime_limits()

    def normalize_runtime_limits(self) -> None:
        """Clamp concurrency limits so config matches admission/backpressure behavior.

        Admission rejects max_concurrency < 1; min_concurrency must not exceed max.
        """
        self.max_concurrency = max(1, self.max_concurrency)
        self.min_concurrency = max(0, min(self.min_concurrency, self.max_concurrency))
        # Fail loudly on a bad scope rather than silently defaulting — a typo'd
        # scope would otherwise flip the limiter semantics unnoticed.
        from ..scheduler.rate_limiter import SCOPES, validate_agent_limits

        if self.rate_limit_scope not in SCOPES:
            raise ValueError(f"Invalid rate_limit_scope {self.rate_limit_scope!r}; expected one of {SCOPES}")
        self.agent_limit_overrides = validate_agent_limits(self.agent_limit_overrides)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "proxy_host": self.proxy_host,
            "proxy_port": self.proxy_port,
            "upstream_url": self.upstream_url,
            "max_concurrency": self.max_concurrency,
            "total_token_budget": self.total_token_budget,
            "default_agent_budget": self.default_agent_budget,
            "aimd_additive_increase": self.aimd_additive_increase,
            "aimd_multiplicative_decrease": self.aimd_multiplicative_decrease,
            "latency_target_ms": self.latency_target_ms,
            "min_concurrency": self.min_concurrency,
            "max_retries": self.max_retries,
            "retry_base_delay": self.retry_base_delay,
            "retry_max_delay": self.retry_max_delay,
            "rpm_limit": self.rpm_limit,
            "tpm_limit": self.tpm_limit,
            "rate_limit_scope": self.rate_limit_scope,
            "agent_limit_overrides": {agent: dict(limits) for agent, limits in self.agent_limit_overrides.items()},
            "db_url": self.db_url,
            "http_tls_verify": self.http_tls_verify,
        }
