"""CLI entry point for running HiveMind benchmarks.

Usage:
    python -m evaluation.run_benchmark                    # Run all scenarios
    python -m evaluation.run_benchmark --scenario micro-5 # Run one scenario
    python -m evaluation.run_benchmark --quick            # Quick smoke test
    python -m evaluation.run_benchmark --ablation         # Run ablation study
    python -m evaluation.run_benchmark --output results.json  # Save JSON
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .harness import BenchmarkHarness
from .reporter import BenchmarkReport
from .scenarios import ALL_SCENARIOS, real_workload_replay


async def run_quick(harness: BenchmarkHarness, report: BenchmarkReport) -> None:
    """Quick smoke test — just the 5-agent micro-benchmark."""
    scenario = ALL_SCENARIOS["micro-5"]()
    direct, hivemind = await harness.run_comparison(scenario)
    report.add_comparison(direct, hivemind)


async def run_micro_benchmarks(harness: BenchmarkHarness, report: BenchmarkReport) -> None:
    """Run all micro-benchmarks (5/10/20/50 agents)."""
    for name in ["micro-5", "micro-10", "micro-20", "micro-50"]:
        scenario = ALL_SCENARIOS[name]()
        direct, hivemind = await harness.run_comparison(scenario)
        report.add_comparison(direct, hivemind)


async def run_replay(harness: BenchmarkHarness, report: BenchmarkReport) -> None:
    """Run the 11-agent replay scenario."""
    scenario = real_workload_replay()
    direct, hivemind = await harness.run_comparison(scenario)
    report.add_comparison(direct, hivemind)


async def run_stress(harness: BenchmarkHarness, report: BenchmarkReport) -> None:
    """Run the stress test."""
    scenario = ALL_SCENARIOS["stress"]()
    direct, hivemind = await harness.run_comparison(scenario)
    report.add_comparison(direct, hivemind)


async def run_ablation(harness: BenchmarkHarness, report: BenchmarkReport) -> None:
    """Run ablation study — each primitive individually."""
    # First, get the baseline (full HiveMind)
    baseline_scenario = real_workload_replay()
    _, baseline = await harness.run_comparison(baseline_scenario)
    report.add_comparison(
        (await harness.run_scenario(real_workload_replay(), use_hivemind=False)),
        baseline,
    )

    # Then each ablation
    for name in [
        "ablation-admission-only",
        "ablation-no-admission",
        "ablation-no-ratelimit",
        "ablation-no-backpressure",
        "ablation-no-retry",
    ]:
        scenario = ALL_SCENARIOS[name]()
        result = await harness.run_scenario(scenario, use_hivemind=True)
        report.add_ablation(result)


async def run_all(harness: BenchmarkHarness, report: BenchmarkReport) -> None:
    """Run the full evaluation suite."""
    await run_micro_benchmarks(harness, report)
    await run_replay(harness, report)
    await run_stress(harness, report)

    # Latency spike
    scenario = ALL_SCENARIOS["latency-spike"]()
    direct, hivemind = await harness.run_comparison(scenario)
    report.add_comparison(direct, hivemind)

    await run_ablation(harness, report)


async def main_async(args: argparse.Namespace) -> None:
    harness = BenchmarkHarness()
    report = BenchmarkReport()

    if args.quick:
        await run_quick(harness, report)
    elif args.scenario:
        if args.scenario not in ALL_SCENARIOS:
            print(f"Unknown scenario: {args.scenario}")
            print(f"Available: {', '.join(ALL_SCENARIOS.keys())}")
            sys.exit(1)
        scenario = ALL_SCENARIOS[args.scenario]()
        direct, hivemind = await harness.run_comparison(scenario)
        report.add_comparison(direct, hivemind)
    elif args.ablation:
        await run_ablation(harness, report)
    elif args.replay:
        await run_replay(harness, report)
    elif args.stress:
        await run_stress(harness, report)
    else:
        await run_all(harness, report)

    # Print results
    print(report.format_table())
    print()
    print(report.paper_summary())

    # Save JSON if requested
    if args.output:
        with open(args.output, "w") as f:
            f.write(report.to_json())
        print(f"\nResults saved to {args.output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="HiveMind Benchmark Runner")
    parser.add_argument("--quick", action="store_true", help="Quick smoke test (5 agents only)")
    parser.add_argument("--scenario", type=str, help="Run a specific scenario")
    parser.add_argument("--ablation", action="store_true", help="Run ablation study")
    parser.add_argument("--replay", action="store_true", help="Run 11-agent replay only")
    parser.add_argument("--stress", action="store_true", help="Run stress test only")
    parser.add_argument("--output", type=str, help="Save JSON results to file")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
