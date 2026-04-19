"""HiveMind CLI entry point.

Usage:
    hivemind                                              # Run MCP server (stdio)
    hivemind serve --upstream https://api.openai.com      # MCP server with OpenAI
    hivemind proxy                                        # Run standalone proxy
    hivemind proxy --upstream https://api.openai.com      # Proxy for OpenAI
    hivemind proxy --max-concurrency 10 --max-retries 5   # Tuned proxy
    hivemind proxy --total-budget 500000                  # With token budget
    hivemind setup cursor                                 # Generate IDE config
    hivemind-proxy --upstream https://api.openai.com      # Same flags as `hivemind proxy`
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .cli_args import (
    apply_serve_cli_args_to_config,
    hivemind_config_from_proxy_cli_args,
    register_proxy_cli_arguments,
    register_serve_cli_arguments,
)
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
    register_serve_cli_arguments(mcp_parser)

    # Standalone proxy (flags shared with `hivemind-proxy`; see `hivemind.cli_args`)
    proxy_parser = subparsers.add_parser("proxy", help="Run standalone API proxy")
    register_proxy_cli_arguments(proxy_parser, include_log_level=False)

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

        config = hivemind_config_from_proxy_cli_args(args)
        run_proxy(config)
    else:
        # Default: run MCP server (bare `hivemind` or `hivemind serve`)
        config = HiveMindConfig()
        apply_serve_cli_args_to_config(config, args)

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
