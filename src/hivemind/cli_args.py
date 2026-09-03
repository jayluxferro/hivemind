"""Shared argparse helpers for HiveMind console entry points (`hivemind`, `hivemind-proxy`)."""

from __future__ import annotations

import argparse
import os

from .scheduler.rate_limiter import AGENT_LIMIT_KINDS
from .storage.models import HiveMindConfig, _default_db_url


def parse_agent_limit_specs(specs: list[str] | None) -> dict[str, dict[str, int]]:
    """Parse repeatable ``--agent-limit AGENT:rpm=N,tpm=M`` specs into a registry.

    Syntax validation lives here at the CLI boundary; the semantic shape is
    re-checked by ``rate_limiter.validate_agent_limits`` when the config is
    normalized, so a bad flag fails loudly either way.
    """
    overrides: dict[str, dict[str, int]] = {}
    for spec in specs or []:
        agent, sep, assignments = spec.partition(":")
        if not sep or not agent.strip() or not assignments.strip():
            raise ValueError(f"Invalid --agent-limit {spec!r}; expected AGENT:rpm=N,tpm=M")
        entry = overrides.setdefault(agent.strip(), {})
        for pair in assignments.split(","):
            kind, eq, raw = pair.partition("=")
            kind = kind.strip()
            if not eq or kind not in AGENT_LIMIT_KINDS:
                raise ValueError(f"Invalid --agent-limit {spec!r}; kinds must be one of {AGENT_LIMIT_KINDS}")
            try:
                value = int(raw)
            except ValueError:
                raise ValueError(f"Invalid --agent-limit {spec!r}; {kind} must be an integer") from None
            if value <= 0:
                raise ValueError(f"Invalid --agent-limit {spec!r}; {kind} must be positive")
            entry[kind] = value
    return overrides


def register_serve_cli_arguments(parser: argparse.ArgumentParser) -> None:
    """Register flags for `hivemind serve` (MCP stdio server tuning)."""
    parser.add_argument(
        "--upstream",
        default="https://api.anthropic.com",
        help="Upstream API URL (auto-detects provider)",
    )
    parser.add_argument("--max-concurrency", type=int, default=5, help="Max concurrent requests")
    parser.add_argument(
        "--db",
        default=_default_db_url(),
        help="Postgres connection string (HIVEMIND_DB_URL env overrides the default)",
    )
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
    parser.add_argument(
        "--rate-limit-scope",
        choices=["per_agent", "global"],
        default=None,
        help="Bucket rate limits per agent session (default) or share one global window",
    )
    parser.add_argument(
        "--agent-limit",
        action="append",
        default=None,
        metavar="AGENT:rpm=N,tpm=M",
        help="Per-agent rate-limit override (repeatable), e.g. --agent-limit batch-bot:rpm=20",
    )


def apply_serve_cli_args_to_config(config: HiveMindConfig, args: argparse.Namespace) -> None:
    """Apply `serve` subparser (or absent subcommand) namespace fields onto an existing config."""
    if hasattr(args, "upstream") and str(getattr(args, "upstream", "")).strip():
        config.upstream_url = str(args.upstream).strip()
    if hasattr(args, "max_concurrency"):
        config.max_concurrency = args.max_concurrency
    if hasattr(args, "db") and str(getattr(args, "db", "")).strip():
        config.db_url = str(args.db).strip()
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
    if getattr(args, "rate_limit_scope", None) is not None:
        config.rate_limit_scope = args.rate_limit_scope
    if getattr(args, "agent_limit", None):
        config.agent_limit_overrides.update(parse_agent_limit_specs(args.agent_limit))
        config.normalize_runtime_limits()  # re-validate the merged registry loudly


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
    parser.add_argument(
        "--db",
        default=_default_db_url(),
        help="Postgres connection string (HIVEMIND_DB_URL env overrides the default)",
    )
    parser.add_argument(
        "--telemetry-dsn",
        default=None,
        metavar="DSN",
        help="Postgres DSN for the token ledger + cost dashboard (MESH_TELEMETRY_DSN env "
        "fallback; unset = telemetry disabled, proxy behavior unchanged)",
    )
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
    parser.add_argument(
        "--rate-limit-scope",
        choices=["per_agent", "global"],
        default=None,
        help="Bucket rate limits per agent session (default) or share one global window",
    )
    parser.add_argument(
        "--agent-limit",
        action="append",
        default=None,
        metavar="AGENT:rpm=N,tpm=M",
        help="Per-agent rate-limit override (repeatable), e.g. --agent-limit batch-bot:rpm=20",
    )


def hivemind_config_from_proxy_cli_args(args: argparse.Namespace) -> HiveMindConfig:
    """Build a `HiveMindConfig` from a namespace produced by `register_proxy_cli_arguments`."""
    upstream_url = str(args.upstream).strip() or "https://api.anthropic.com"
    db_url = str(args.db).strip() or _default_db_url()
    proxy_host = str(args.host).strip() or "127.0.0.1"

    config = HiveMindConfig(
        proxy_host=proxy_host,
        proxy_port=args.port,
        upstream_url=upstream_url,
        max_concurrency=args.max_concurrency,
        min_concurrency=args.min_concurrency,
        db_url=db_url,
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
    if getattr(args, "rate_limit_scope", None) is not None:
        config.rate_limit_scope = args.rate_limit_scope
    if getattr(args, "agent_limit", None):
        config.agent_limit_overrides.update(parse_agent_limit_specs(args.agent_limit))
        config.normalize_runtime_limits()  # re-validate the merged registry loudly

    # Token ledger: explicit flag wins; MESH_TELEMETRY_DSN env is the fallback.
    # Neither set → telemetry_dsn stays None → NullLedger (zero behavior change).
    telemetry_dsn = getattr(args, "telemetry_dsn", None) or os.environ.get("MESH_TELEMETRY_DSN")
    if telemetry_dsn:
        config.telemetry_dsn = str(telemetry_dsn).strip() or None
    return config
