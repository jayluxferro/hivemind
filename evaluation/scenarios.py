"""Pre-defined benchmark scenarios from the BLUEPRINT evaluation plan.

Scenarios:
1. Micro-benchmark: 5/10/20/50 concurrent agents
2. Real workload replay: 11 agents (the original failure case)
3. Stress test: rate limit exhaustion and recovery
4. Ablation: each primitive individually
"""

from __future__ import annotations

from .harness import ScenarioConfig


def micro_benchmark_5() -> ScenarioConfig:
    """5 concurrent agents — should work fine even without HiveMind."""
    return ScenarioConfig(
        name="micro-5",
        description="5 concurrent agents, moderate rate limit",
        num_agents=5,
        agent_turns=8,
        api_requests_per_minute=50,
        api_base_latency_ms=80.0,
        hm_max_concurrency=5,
    )


def micro_benchmark_10() -> ScenarioConfig:
    """10 concurrent agents — starting to see contention."""
    return ScenarioConfig(
        name="micro-10",
        description="10 concurrent agents, moderate rate limit",
        num_agents=10,
        agent_turns=8,
        api_requests_per_minute=50,
        api_base_latency_ms=80.0,
        hm_max_concurrency=5,
    )


def micro_benchmark_20() -> ScenarioConfig:
    """20 concurrent agents — significant contention."""
    return ScenarioConfig(
        name="micro-20",
        description="20 concurrent agents, moderate rate limit",
        num_agents=20,
        agent_turns=8,
        api_requests_per_minute=50,
        api_base_latency_ms=80.0,
        hm_max_concurrency=5,
    )


def micro_benchmark_50() -> ScenarioConfig:
    """50 concurrent agents — extreme contention."""
    return ScenarioConfig(
        name="micro-50",
        description="50 concurrent agents, moderate rate limit",
        num_agents=50,
        agent_turns=6,
        api_requests_per_minute=50,
        api_base_latency_ms=80.0,
        hm_max_concurrency=5,
    )


def real_workload_replay() -> ScenarioConfig:
    """Replay the original 11-agent IDLEx scenario that motivated HiveMind.

    11 agents, moderate rate limit with concurrency cap and error injection.
    The concurrency limit + errors cause ~25-35% agent death rate when
    all 11 agents stampede simultaneously.
    """
    return ScenarioConfig(
        name="replay-11",
        description="11 agents replaying the original failure scenario",
        num_agents=11,
        agent_turns=8,
        api_requests_per_minute=60,  # Moderate — enough for all if staggered
        api_tokens_per_minute=200_000,
        api_error_rate=0.08,  # 8% random 502s
        api_connection_reset_rate=0.05,  # 5% connection resets
        api_base_latency_ms=120.0,
        api_latency_jitter_ms=60.0,
        api_max_concurrent=5,  # Hard concurrency cap — the key bottleneck
        hm_max_concurrency=5,
    )


def stress_test() -> ScenarioConfig:
    """Deliberate rate limit exhaustion — how well does HiveMind recover?"""
    return ScenarioConfig(
        name="stress",
        description="20 agents, very tight rate limit, high error rate",
        num_agents=20,
        agent_turns=10,
        api_requests_per_minute=20,  # Very tight
        api_tokens_per_minute=50_000,
        api_error_rate=0.10,  # 10% 502s
        api_connection_reset_rate=0.05,  # 5% resets
        api_base_latency_ms=200.0,
        api_latency_spike_rate=0.1,
        api_max_concurrent=3,
        hm_max_concurrency=3,
    )


def latency_spike_test() -> ScenarioConfig:
    """Tests AIMD backpressure under latency spikes."""
    return ScenarioConfig(
        name="latency-spike",
        description="10 agents with frequent latency spikes",
        num_agents=10,
        agent_turns=10,
        api_requests_per_minute=60,
        api_base_latency_ms=100.0,
        api_latency_jitter_ms=30.0,
        api_latency_spike_rate=0.2,  # 20% of requests spike to 5x latency
        hm_max_concurrency=5,
    )


# --- Ablation studies: disable one primitive at a time ---

def ablation_no_admission() -> ScenarioConfig:
    """HiveMind without admission control."""
    s = real_workload_replay()
    s.name = "ablation-no-admission"
    s.description = "HiveMind with admission control disabled"
    s.enable_admission = False
    return s


def ablation_no_rate_limiter() -> ScenarioConfig:
    """HiveMind without rate limit tracking."""
    s = real_workload_replay()
    s.name = "ablation-no-ratelimit"
    s.description = "HiveMind with rate limit tracking disabled"
    s.enable_rate_limiter = False
    return s


def ablation_no_backpressure() -> ScenarioConfig:
    """HiveMind without AIMD backpressure."""
    s = real_workload_replay()
    s.name = "ablation-no-backpressure"
    s.description = "HiveMind with backpressure disabled"
    s.enable_backpressure = False
    return s


def ablation_no_retry() -> ScenarioConfig:
    """HiveMind without transparent retry."""
    s = real_workload_replay()
    s.name = "ablation-no-retry"
    s.description = "HiveMind with retry disabled"
    s.enable_retry = False
    return s


def ablation_admission_only() -> ScenarioConfig:
    """HiveMind with ONLY admission control (the claim: this alone fixes 80%+)."""
    s = real_workload_replay()
    s.name = "ablation-admission-only"
    s.description = "HiveMind with only admission control enabled"
    s.enable_rate_limiter = False
    s.enable_backpressure = False
    s.enable_budget = False
    s.enable_retry = False
    return s


# All scenarios in evaluation order
ALL_SCENARIOS = {
    "micro-5": micro_benchmark_5,
    "micro-10": micro_benchmark_10,
    "micro-20": micro_benchmark_20,
    "micro-50": micro_benchmark_50,
    "replay-11": real_workload_replay,
    "stress": stress_test,
    "latency-spike": latency_spike_test,
    "ablation-no-admission": ablation_no_admission,
    "ablation-no-ratelimit": ablation_no_rate_limiter,
    "ablation-no-backpressure": ablation_no_backpressure,
    "ablation-no-retry": ablation_no_retry,
    "ablation-admission-only": ablation_admission_only,
}
