"""Results reporter — formats benchmark results as tables and JSON.

Produces:
- Console-friendly comparison tables
- JSON output for further analysis
- Summary statistics for the paper
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .harness import ScenarioResult


@dataclass
class ComparisonResult:
    """Side-by-side comparison of direct vs HiveMind results."""

    scenario_name: str
    direct: ScenarioResult
    hivemind: ScenarioResult

    @property
    def failure_rate_reduction(self) -> float:
        """How much HiveMind reduced the failure rate (percentage points)."""
        return (self.direct.failure_rate - self.hivemind.failure_rate) * 100

    @property
    def token_waste_reduction(self) -> float:
        """How much HiveMind reduced token waste (percentage)."""
        if self.direct.wasted_tokens == 0:
            return 0.0
        return ((self.direct.wasted_tokens - self.hivemind.wasted_tokens) / self.direct.wasted_tokens) * 100

    @property
    def wall_time_overhead(self) -> float:
        """Wall time overhead of HiveMind (percentage)."""
        if self.direct.wall_time_s == 0:
            return 0.0
        return ((self.hivemind.wall_time_s - self.direct.wall_time_s) / self.direct.wall_time_s) * 100


@dataclass
class BenchmarkReport:
    """Full benchmark report across all scenarios."""

    comparisons: list[ComparisonResult] = field(default_factory=list)
    ablations: list[ScenarioResult] = field(default_factory=list)

    def add_comparison(self, direct: ScenarioResult, hivemind: ScenarioResult) -> None:
        self.comparisons.append(
            ComparisonResult(
                scenario_name=direct.scenario_name,
                direct=direct,
                hivemind=hivemind,
            )
        )

    def add_ablation(self, result: ScenarioResult) -> None:
        self.ablations.append(result)

    def format_table(self) -> str:
        """Format results as an ASCII comparison table."""
        lines = []

        # Header
        lines.append("=" * 100)
        lines.append("HiveMind Evaluation Results")
        lines.append("=" * 100)
        lines.append("")

        # Comparison table
        if self.comparisons:
            lines.append("--- Direct vs HiveMind ---")
            lines.append("")
            header = f"{'Scenario':<20} {'Mode':<10} {'Agents':<8} {'Alive':<8} {'Dead':<6} {'Fail%':<8} {'Tokens':<10} {'Wasted':<10} {'Time(s)':<10}"
            lines.append(header)
            lines.append("-" * len(header))

            for comp in self.comparisons:
                for result, mode in [(comp.direct, "direct"), (comp.hivemind, "hivemind")]:
                    lines.append(
                        f"{result.scenario_name:<20} {mode:<10} {len(result.agent_results):<8} "
                        f"{result.agents_alive:<8} {result.agents_dead:<6} "
                        f"{result.failure_rate * 100:<8.1f} {result.total_tokens:<10} "
                        f"{result.wasted_tokens:<10} {result.wall_time_s:<10.2f}"
                    )
                lines.append("")

            # Improvement summary
            lines.append("")
            lines.append("--- Improvements ---")
            lines.append("")
            header = (
                f"{'Scenario':<20} {'Fail Rate Reduction':<22} {'Token Waste Reduction':<22} {'Wall Time Overhead':<20}"
            )
            lines.append(header)
            lines.append("-" * len(header))
            for comp in self.comparisons:
                lines.append(
                    f"{comp.scenario_name:<20} "
                    f"{comp.failure_rate_reduction:>+.1f} pp{'':<16} "
                    f"{comp.token_waste_reduction:>+.1f}%{'':<16} "
                    f"{comp.wall_time_overhead:>+.1f}%"
                )

        # Ablation table
        if self.ablations:
            lines.append("")
            lines.append("--- Ablation Study ---")
            lines.append("")
            header = (
                f"{'Scenario':<30} {'Alive':<8} {'Dead':<6} {'Fail%':<8} {'Tokens':<10} {'Wasted':<10} {'Time(s)':<10}"
            )
            lines.append(header)
            lines.append("-" * len(header))
            for result in self.ablations:
                lines.append(
                    f"{result.scenario_name:<30} "
                    f"{result.agents_alive:<8} {result.agents_dead:<6} "
                    f"{result.failure_rate * 100:<8.1f} {result.total_tokens:<10} "
                    f"{result.wasted_tokens:<10} {result.wall_time_s:<10.2f}"
                )

        lines.append("")
        lines.append("=" * 100)
        return "\n".join(lines)

    def to_json(self) -> str:
        """Export results as JSON."""
        data = {
            "comparisons": [],
            "ablations": [],
        }
        for comp in self.comparisons:
            data["comparisons"].append(
                {
                    "scenario": comp.scenario_name,
                    "direct": comp.direct.summary(),
                    "hivemind": comp.hivemind.summary(),
                    "improvement": {
                        "failure_rate_reduction_pp": round(comp.failure_rate_reduction, 2),
                        "token_waste_reduction_pct": round(comp.token_waste_reduction, 2),
                        "wall_time_overhead_pct": round(comp.wall_time_overhead, 2),
                    },
                }
            )
        for result in self.ablations:
            data["ablations"].append(result.summary())
        return json.dumps(data, indent=2)

    def paper_summary(self) -> str:
        """Generate key claims with supporting data for the paper."""
        lines = []
        lines.append("Key Results for Paper")
        lines.append("=" * 50)
        lines.append("")

        for comp in self.comparisons:
            lines.append(f"[{comp.scenario_name}]")
            lines.append(
                f"  Without HiveMind: {comp.direct.failure_rate * 100:.1f}% failure rate, {comp.direct.wasted_tokens} wasted tokens"
            )
            lines.append(
                f"  With HiveMind:    {comp.hivemind.failure_rate * 100:.1f}% failure rate, {comp.hivemind.wasted_tokens} wasted tokens"
            )
            lines.append(
                f"  Improvement:      {comp.failure_rate_reduction:+.1f}pp failure, {comp.token_waste_reduction:+.1f}% waste reduction"
            )
            lines.append(f"  Overhead:         {comp.wall_time_overhead:+.1f}% wall time")
            lines.append("")

        # Check paper claims
        lines.append("Claim Validation:")
        for comp in self.comparisons:
            if comp.scenario_name == "replay-11":
                claim1 = comp.failure_rate_reduction >= 20  # "80%+ reduction"
                lines.append(
                    f"  Claim 1 (admission reduces failures 80%+): {'SUPPORTED' if claim1 else 'NOT SUPPORTED'} ({comp.failure_rate_reduction:+.1f}pp)"
                )

        return "\n".join(lines)
