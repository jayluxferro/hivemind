"""Tests for the MCP config generator."""

import json
import pytest

from hivemind.tools.setup import SUPPORTED_TOOLS, generate_config, setup_tool


def test_supported_tools_list():
    assert "claude-code" in SUPPORTED_TOOLS
    assert "cursor" in SUPPORTED_TOOLS
    assert "windsurf" in SUPPORTED_TOOLS
    assert "codex" in SUPPORTED_TOOLS
    assert "copilot" in SUPPORTED_TOOLS
    assert "generic" in SUPPORTED_TOOLS


def test_generate_claude_code_config():
    result = generate_config("claude-code")
    assert "config" in result
    assert "instructions" in result
    assert "mcpServers" in result["config"]
    assert "hivemind" in result["config"]["mcpServers"]


def test_generate_cursor_config():
    result = generate_config("cursor")
    assert "config" in result
    assert "mcpServers" in result["config"]
    assert result["path"] == ".cursor/mcp.json"


def test_generate_windsurf_config():
    result = generate_config("windsurf")
    assert "config" in result
    assert "windsurf" in result["path"].lower() or "codeium" in result["path"].lower()


def test_generate_codex_config():
    result = generate_config("codex")
    assert "instructions" in result
    assert any("OPENAI_BASE_URL" in line for line in result["instructions"])


def test_generate_copilot_config():
    result = generate_config("copilot")
    assert "instructions" in result


def test_generate_generic_config():
    result = generate_config("generic")
    assert "ANTHROPIC_BASE_URL" in result["config"]
    assert "OPENAI_BASE_URL" in result["config"]


def test_custom_port():
    result = generate_config("generic", proxy_port=9999)
    assert "9999" in result["config"]["ANTHROPIC_BASE_URL"]


def test_unknown_tool():
    result = generate_config("unknown_tool_xyz")
    assert "error" in result


@pytest.mark.asyncio
async def test_setup_tool_list():
    result = await setup_tool(tool="list")
    assert "supported_tools" in result
    assert len(result["supported_tools"]) >= 6


@pytest.mark.asyncio
async def test_setup_tool_generic():
    result = await setup_tool(tool="generic")
    assert "instructions" in result


def test_config_contains_valid_json():
    """Ensure generated MCP configs are valid JSON-serializable."""
    for tool_name in ["claude-code", "cursor", "windsurf"]:
        result = generate_config(tool_name)
        config = result["config"]
        # Should be JSON-serializable
        serialized = json.dumps(config)
        assert len(serialized) > 10
