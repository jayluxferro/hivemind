"""Shared argparse helpers for HiveMind console entry points (`hivemind`, `hivemind-proxy`)."""

from __future__ import annotations

import argparse

from .storage.models import HiveMindConfig


def register_serve_cli_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags for `hivemind serve` (MCP stdio server tuning)."""
    parser.add_argument(
        "--upstream",
        default="https://api.anthropic.com",
        help="Upstream API URL (auto-detects provider)",
    )
    parser.add_argument("--max-concurrency", type=int, default=5, help="Max concurrent requests")
    parser.add_argument("--db", default="hivemind.db", help="Database path")
    parser.add_argument(
        "--total-budget",
        type=int,
        default=None,
        help="Global token budget (default: unlimited)",
    )
    parser.add_argument(
        "--agent-budget",
        type=int,
        default=None,
        help="Default per-agent token budget (default: unlimited)",
    )
    parser.add_argument("--max-retries", type=int, default=3, help="Max transparent retries on 429/502")
    parser.add_argument("--min-concurrency", type=int, default=1, help="Floor for AIMD backpressure")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable upstream HTTPS TLS certificate verification",
    )
    parser.add_argument(
        "--rpm-limit",
        type=int,
        default=None,
        help="Override requests-per-minute limit (auto-detected from provider)",
    )
    parser.add_argument(
        "--tpm-limit",
        type=int,
        default=None,
        help="Override tokens-per-minute limit (auto-detected from provider)",
    )


def apply_serve_cli_args_to_config(config: HiveMindConfig, args: argparse.Namespace) -> None:
    """Apply `serve` subparser (or absent subcommand) namespace fields onto an existing config."""
    if hasattr(args, "upstream") and str(getattr(args, "upstream", "")).strip():
        config.upstream_url = str(args.upstream).strip()
    if hasattr(args, "max_concurrency"):
        config.max_concurrency = args.max_concurrency
    if hasattr(args, "db") and str(getattr(args, "db", "")).strip():
        config.db_path = str(args.db).strip()
    if hasattr(args, "total_budget") and args.total_budget is not None:
        config.total_token_budget = args.total_budget
    if hasattr(args, "agent_budget") and args.agent_budget is not None:
        config.default_agent_budget = args.agent_budget
    if hasattr(args, "max_retries"):
        config.max_retries = args.max_retries
    if hasattr(args, "min_concurrency"):
        config.min_concurrency = args.min_concurrency
    if hasattr(args, "insecure") and args.insecure:
        config.http_tls_verify = False
    if getattr(args, "rpm_limit", None) is not None:
        config.rpm_limit = args.rpm_limit
    if getattr(args, "tpm_limit", None) is not None:
        config.tpm_limit = args.tpm_limit


def register_proxy_cli_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_log_level: bool = False,
) -> None:
    """Register all standalone-proxy flags (parity between `hivemind proxy` and `hivemind-proxy`)."""
    if include_log_level:
        parser.add_argument(
            "--log-level",
            default="INFO",
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            help="Logging verbosity",
        )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument(
        "--upstream",
        default="https://api.anthropic.com",
        help="Upstream API URL (auto-detects provider)",
    )
    parser.add_argument("--max-concurrency", type=int, default=5, help="Max concurrent requests")
    parser.add_argument("--min-concurrency", type=int, default=1, help="Floor for AIMD backpressure")
    parser.add_argument("--db", default="hivemind.db", help="SQLite database path")
    parser.add_argument("--max-retries", type=int, default=3, help="Max transparent retries on 429/502")
    parser.add_argument("--retry-base-delay", type=float, default=1.0, help="Base retry delay in seconds")
    parser.add_argument("--retry-max-delay", type=float, default=30.0, help="Max retry delay in seconds")
    parser.add_argument(
        "--latency-target",
        type=float,
        default=None,
        help="Latency target in ms for AIMD (auto-detected from provider)",
    )
    parser.add_argument(
        "--aimd-increase",
        type=float,
        default=None,
        help="AIMD additive increase (auto-detected from provider)",
    )
    parser.add_argument(
        "--aimd-decrease",
        type=float,
        default=None,
        help="AIMD multiplicative decrease (auto-detected from provider)",
    )
    parser.add_argument("--total-budget", type=int, default=None, help="Global token budget (default: unlimited)")
    parser.add_argument(
        "--agent-budget",
        type=int,
        default=None,
        help="Default per-agent token budget (default: unlimited)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable upstream HTTPS TLS certificate verification",
    )
    parser.add_argument(
        "--rpm-limit",
        type=int,
        default=None,
        help="Override requests-per-minute limit (auto-detected from provider)",
    )
    parser.add_argument(
        "--tpm-limit",
        type=int,
        default=None,
        help="Override tokens-per-minute limit (auto-detected from provider)",
    )


def hivemind_config_from_proxy_cli_args(args: argparse.Namespace) -> HiveMindConfig:
    """Build a `HiveMindConfig` from a namespace produced by `register_proxy_cli_arguments`."""
    upstream_url = str(args.upstream).strip() or "https://api.anthropic.com"
    db_path = str(args.db).strip() or "hivemind.db"
    proxy_host = str(args.host).strip() or "127.0.0.1"

    config = HiveMindConfig(
        proxy_host=proxy_host,
        proxy_port=args.port,
        upstream_url=upstream_url,
        max_concurrency=args.max_concurrency,
        min_concurrency=args.min_concurrency,
        db_path=db_path,
        max_retries=args.max_retries,
        retry_base_delay=args.retry_base_delay,
        retry_max_delay=args.retry_max_delay,
        http_tls_verify=not getattr(args, "insecure", False),
    )
    if args.total_budget is not None:
        config.total_token_budget = args.total_budget
    if args.agent_budget is not None:
        config.default_agent_budget = args.agent_budget
    if args.latency_target is not None:
        config.latency_target_ms = args.latency_target
    if args.aimd_increase is not None:
        config.aimd_additive_increase = args.aimd_increase
    if args.aimd_decrease is not None:
        config.aimd_multiplicative_decrease = args.aimd_decrease
    if getattr(args, "rpm_limit", None) is not None:
        config.rpm_limit = args.rpm_limit
    if getattr(args, "tpm_limit", None) is not None:
        config.tpm_limit = args.tpm_limit
    return config
