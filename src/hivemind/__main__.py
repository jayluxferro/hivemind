"""HiveMind CLI entry point.

Usage:
    hivemind                                              # Run MCP server (stdio)
    hivemind serve --upstream https://api.openai.com      # MCP server with OpenAI
    hivemind proxy                                        # Run standalone proxy
    hivemind proxy --upstream https://api.openai.com      # Proxy for OpenAI
    hivemind proxy --max-concurrency 10 --max-retries 5   # Tuned proxy
    hivemind proxy --total-budget 500000                  # With token budget
    hivemind setup cursor                                 # Generate IDE config
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .storage.models import HiveMindConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hivemind",
        description="OS-inspired scheduler for concurrent LLM coding agents",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    subparsers = parser.add_subparsers(dest="command")

    # MCP server (default)
    mcp_parser = subparsers.add_parser("serve", help="Run MCP server (default)")
    mcp_parser.add_argument("--upstream", default="https://api.anthropic.com", help="Upstream API URL (auto-detects provider)")
    mcp_parser.add_argument("--max-concurrency", type=int, default=5, help="Max concurrent requests")
    mcp_parser.add_argument("--db", default="hivemind.db", help="Database path")
    mcp_parser.add_argument("--total-budget", type=int, default=None, help="Global token budget (default: unlimited)")
    mcp_parser.add_argument("--agent-budget", type=int, default=None, help="Default per-agent token budget (default: unlimited)")
    mcp_parser.add_argument("--max-retries", type=int, default=3, help="Max transparent retries on 429/502")
    mcp_parser.add_argument("--min-concurrency", type=int, default=1, help="Floor for AIMD backpressure")

    # Standalone proxy
    proxy_parser = subparsers.add_parser("proxy", help="Run standalone API proxy")
    proxy_parser.add_argument("--host", default="127.0.0.1", help="Bind address")
    proxy_parser.add_argument("--port", type=int, default=8765, help="Bind port")
    proxy_parser.add_argument("--upstream", default="https://api.anthropic.com", help="Upstream API URL (auto-detects provider)")
    proxy_parser.add_argument("--max-concurrency", type=int, default=5, help="Max concurrent requests")
    proxy_parser.add_argument("--min-concurrency", type=int, default=1, help="Floor for AIMD backpressure")
    proxy_parser.add_argument("--db", default="hivemind.db", help="Database path")
    # Retry
    proxy_parser.add_argument("--max-retries", type=int, default=3, help="Max transparent retries on 429/502")
    proxy_parser.add_argument("--retry-base-delay", type=float, default=1.0, help="Base retry delay in seconds")
    proxy_parser.add_argument("--retry-max-delay", type=float, default=30.0, help="Max retry delay in seconds")
    # Backpressure
    proxy_parser.add_argument("--latency-target", type=float, default=None, help="Latency target in ms for AIMD (auto-detected from provider)")
    proxy_parser.add_argument("--aimd-increase", type=float, default=None, help="AIMD additive increase (auto-detected from provider)")
    proxy_parser.add_argument("--aimd-decrease", type=float, default=None, help="AIMD multiplicative decrease (auto-detected from provider)")
    # Token budgets
    proxy_parser.add_argument("--total-budget", type=int, default=None, help="Global token budget (default: unlimited)")
    proxy_parser.add_argument("--agent-budget", type=int, default=None, help="Default per-agent token budget (default: unlimited)")

    # Setup config generator
    setup_parser = subparsers.add_parser("setup", help="Generate config for AI coding tools")
    setup_parser.add_argument(
        "tool",
        nargs="?",
        default="generic",
        choices=["claude-code", "cursor", "windsurf", "codex", "copilot", "generic", "all"],
        help="Target tool (default: generic)",
    )
    setup_parser.add_argument("--port", type=int, default=8765)
    setup_parser.add_argument("--upstream", default="https://api.anthropic.com")
    setup_parser.add_argument("--max-concurrency", type=int, default=5)

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.command == "setup":
        from .tools.setup import generate_config, SUPPORTED_TOOLS

        tools_to_show = SUPPORTED_TOOLS if args.tool == "all" else [args.tool]
        for tool_name in tools_to_show:
            result = generate_config(
                tool=tool_name,
                proxy_port=args.port,
                upstream_url=args.upstream,
                max_concurrency=args.max_concurrency,
            )
            print(f"\n{'=' * 60}")
            print(f"  {tool_name.upper()}")
            print(f"{'=' * 60}")
            for line in result.get("instructions", []):
                print(f"  {line}")
            if result.get("path"):
                print(f"\n  Config path: {result['path']}")
            print()
        return

    elif args.command == "proxy":
        from .proxy.server import run_proxy

        config = HiveMindConfig(
            proxy_host=args.host,
            proxy_port=args.port,
            upstream_url=args.upstream,
            max_concurrency=args.max_concurrency,
            min_concurrency=args.min_concurrency,
            db_path=args.db,
            max_retries=args.max_retries,
            retry_base_delay=args.retry_base_delay,
            retry_max_delay=args.retry_max_delay,
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

        run_proxy(config)
    else:
        # Default: run MCP server
        config = HiveMindConfig()
        if hasattr(args, "upstream") and args.upstream:
            config.upstream_url = args.upstream
        if hasattr(args, "max_concurrency") and args.max_concurrency:
            config.max_concurrency = args.max_concurrency
        if hasattr(args, "db") and args.db:
            config.db_path = args.db
        if hasattr(args, "total_budget") and args.total_budget:
            config.total_token_budget = args.total_budget
        if hasattr(args, "agent_budget") and args.agent_budget:
            config.default_agent_budget = args.agent_budget
        if hasattr(args, "max_retries") and args.max_retries:
            config.max_retries = args.max_retries
        if hasattr(args, "min_concurrency") and args.min_concurrency:
            config.min_concurrency = args.min_concurrency

        from .server import run_server

        try:
            asyncio.run(run_server(config))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
