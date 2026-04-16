"""hm.setup — Generate configuration for AI coding tools.

Produces ready-to-paste config snippets for:
- Claude Code (settings.json)
- Cursor (.cursor/mcp.json)
- Windsurf
- Codex CLI
- GitHub Copilot
- Generic (environment variables)
"""

from __future__ import annotations

import json
import os
import sys

SUPPORTED_TOOLS = [
    "claude-code",
    "cursor",
    "windsurf",
    "codex",
    "copilot",
    "generic",
]


def get_hivemind_command() -> list[str]:
    """Get the command to run HiveMind MCP server."""
    # Check if hivemind is installed as a script
    return ["hivemind", "serve"]


def generate_config(
    tool: str,
    proxy_host: str = "127.0.0.1",
    proxy_port: int = 8765,
    upstream_url: str = "https://api.anthropic.com",
    max_concurrency: int = 5,
) -> dict:
    """Generate configuration for a specific AI coding tool.

    Args:
        tool: Target tool name
        proxy_host: HiveMind proxy host
        proxy_port: HiveMind proxy port
        upstream_url: Upstream API URL
        max_concurrency: Max concurrent requests

    Returns:
        Dict with 'config' (the snippet), 'path' (where to put it),
        and 'instructions' (human-readable setup steps)
    """
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    python = sys.executable

    if tool == "claude-code":
        return _claude_code_config(proxy_url, python, upstream_url, max_concurrency)
    elif tool == "cursor":
        return _cursor_config(proxy_url, python, upstream_url, max_concurrency)
    elif tool == "windsurf":
        return _windsurf_config(proxy_url, python, upstream_url, max_concurrency)
    elif tool == "codex":
        return _codex_config(proxy_url, proxy_host, proxy_port)
    elif tool == "copilot":
        return _copilot_config(proxy_url, proxy_host, proxy_port)
    elif tool == "generic":
        return _generic_config(proxy_url, proxy_host, proxy_port)
    else:
        return {"error": f"Unknown tool '{tool}'. Supported: {', '.join(SUPPORTED_TOOLS)}"}


def _claude_code_config(proxy_url: str, python: str, upstream: str, concurrency: int) -> dict:
    config = {
        "mcpServers": {
            "hivemind": {
                "command": python,
                "args": ["-m", "hivemind", "serve", "--upstream", upstream, "--max-concurrency", str(concurrency)],
            }
        }
    }
    env_config = {
        "env": {
            "ANTHROPIC_BASE_URL": proxy_url,
        }
    }
    return {
        "config": config,
        "path": "~/.claude/settings.json (merge into mcpServers)",
        "env": env_config,
        "instructions": [
            "Option A: MCP Server (rich tools — submit, status, metrics, etc.)",
            f"  Add to ~/.claude/settings.json:",
            f"  {json.dumps(config, indent=2)}",
            "",
            "Option B: Transparent proxy (zero config, just set env var)",
            f"  1. Start proxy: hivemind proxy --port {proxy_url.split(':')[-1]}",
            f"  2. Set env: export ANTHROPIC_BASE_URL={proxy_url}",
            "  3. Run claude as normal — all API calls route through HiveMind",
        ],
    }


def _cursor_config(proxy_url: str, python: str, upstream: str, concurrency: int) -> dict:
    config = {
        "mcpServers": {
            "hivemind": {
                "command": python,
                "args": ["-m", "hivemind", "serve", "--upstream", upstream, "--max-concurrency", str(concurrency)],
            }
        }
    }
    return {
        "config": config,
        "path": ".cursor/mcp.json",
        "instructions": [
            "Add to .cursor/mcp.json in your project root:",
            json.dumps(config, indent=2),
            "",
            "Or use transparent proxy mode:",
            f"  1. Start: hivemind proxy",
            f"  2. In Cursor settings, set API base URL to {proxy_url}",
        ],
    }


def _windsurf_config(proxy_url: str, python: str, upstream: str, concurrency: int) -> dict:
    config = {
        "mcpServers": {
            "hivemind": {
                "command": python,
                "args": ["-m", "hivemind", "serve", "--upstream", upstream, "--max-concurrency", str(concurrency)],
            }
        }
    }
    return {
        "config": config,
        "path": "~/.codeium/windsurf/mcp_config.json",
        "instructions": [
            "Add to ~/.codeium/windsurf/mcp_config.json:",
            json.dumps(config, indent=2),
        ],
    }


def _codex_config(proxy_url: str, host: str, port: int) -> dict:
    return {
        "config": {"ANTHROPIC_BASE_URL": proxy_url},
        "path": "Environment variable",
        "instructions": [
            "1. Start HiveMind proxy:",
            f"   hivemind proxy --host {host} --port {port}",
            "",
            "2. Run Codex with proxy:",
            f"   ANTHROPIC_BASE_URL={proxy_url} codex",
            "",
            "All Codex agents will route through HiveMind automatically.",
        ],
    }


def _copilot_config(proxy_url: str, host: str, port: int) -> dict:
    return {
        "config": {"proxy_url": proxy_url},
        "path": "VS Code settings.json",
        "instructions": [
            "1. Start HiveMind proxy:",
            f"   hivemind proxy --host {host} --port {port}",
            "",
            "2. In VS Code settings.json, add:",
            '   "http.proxy": "%s"' % proxy_url,
            "",
            "Or set environment variable:",
            f"   HTTP_PROXY={proxy_url}",
        ],
    }


def _generic_config(proxy_url: str, host: str, port: int) -> dict:
    return {
        "config": {
            "ANTHROPIC_BASE_URL": proxy_url,
            "OPENAI_BASE_URL": f"{proxy_url}/v1",
        },
        "path": "Environment variables",
        "instructions": [
            "1. Start HiveMind proxy:",
            f"   hivemind proxy --host {host} --port {port}",
            "",
            "2. Point your agents at the proxy:",
            f"   export ANTHROPIC_BASE_URL={proxy_url}",
            f"   export OPENAI_BASE_URL={proxy_url}/v1",
            "",
            "3. Run agents as normal — all API calls route through HiveMind.",
            "",
            "Works with: Claude Code, LangChain, CrewAI, AutoGen, raw SDKs, curl, etc.",
        ],
    }


async def setup_tool(
    tool: str = "generic",
    proxy_host: str = "127.0.0.1",
    proxy_port: int = 8765,
    upstream_url: str = "https://api.anthropic.com",
    max_concurrency: int = 5,
) -> dict:
    """MCP tool handler for hm.setup."""
    if tool == "list":
        return {"supported_tools": SUPPORTED_TOOLS}

    result = generate_config(
        tool=tool,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        upstream_url=upstream_url,
        max_concurrency=max_concurrency,
    )
    return result
