"""HiveMind CLI entry point.

Usage:
    hivemind                   # Run MCP server (stdio)
    hivemind proxy             # Run standalone proxy
    hivemind proxy --port 8765 # Run proxy on custom port
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

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
    mcp_parser.add_argument("--db", default="hivemind.db", help="Database path")
    mcp_parser.add_argument("--max-concurrency", type=int, default=5)
    mcp_parser.add_argument("--upstream", default="https://api.anthropic.com")
    mcp_parser.add_argument("--total-budget", type=int, default=None)

    # Standalone proxy
    proxy_parser = subparsers.add_parser("proxy", help="Run standalone API proxy")
    proxy_parser.add_argument("--host", default="127.0.0.1")
    proxy_parser.add_argument("--port", type=int, default=8765)
    proxy_parser.add_argument("--upstream", default="https://api.anthropic.com")
    proxy_parser.add_argument("--max-concurrency", type=int, default=5)
    proxy_parser.add_argument("--db", default="hivemind.db")

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
        import json
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
        from .proxy.server import main as proxy_main

        # Override sys.argv for the proxy's argparse
        sys.argv = ["hivemind-proxy"]
        if args.host:
            sys.argv.extend(["--host", args.host])
        if args.port:
            sys.argv.extend(["--port", str(args.port)])
        if args.upstream:
            sys.argv.extend(["--upstream", args.upstream])
        if args.max_concurrency:
            sys.argv.extend(["--max-concurrency", str(args.max_concurrency)])
        if args.db:
            sys.argv.extend(["--db", args.db])
        proxy_main()
    else:
        # Default: run MCP server
        config = HiveMindConfig()
        if hasattr(args, "db") and args.db:
            config.db_path = args.db
        if hasattr(args, "max_concurrency") and args.max_concurrency:
            config.max_concurrency = args.max_concurrency
        if hasattr(args, "upstream") and args.upstream:
            config.upstream_url = args.upstream
        if hasattr(args, "total_budget") and args.total_budget:
            config.total_token_budget = args.total_budget

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
