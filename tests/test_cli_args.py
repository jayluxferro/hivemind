"""Tests for `hivemind.cli_args` (shared CLI for proxy and serve)."""

import argparse

import pytest

from hivemind.cli_args import (
    apply_serve_cli_args_to_config,
    hivemind_config_from_proxy_cli_args,
    parse_agent_limit_specs,
    register_proxy_cli_arguments,
    register_serve_cli_arguments,
)
from hivemind.storage.models import HiveMindConfig


def test_proxy_cli_zero_budgets_and_retries():
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    args = parser.parse_args(
        ["--total-budget", "0", "--agent-budget", "100", "--max-retries", "0"],
    )
    c = hivemind_config_from_proxy_cli_args(args)
    assert c.total_token_budget == 0
    assert c.default_agent_budget == 100
    assert c.max_retries == 0


def test_proxy_cli_insecure_disables_tls_verify():
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    args = parser.parse_args(["--insecure"])
    c = hivemind_config_from_proxy_cli_args(args)
    assert c.http_tls_verify is False


def test_apply_serve_cli_args_bare_namespace_noop():
    """Bare `hivemind` has no serve-specific attributes on the namespace."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args([])
    c = HiveMindConfig()
    apply_serve_cli_args_to_config(c, args)
    assert c.upstream_url == "https://api.anthropic.com"


def test_apply_serve_cli_args_from_serve_parser():
    parser = argparse.ArgumentParser()
    register_serve_cli_arguments(parser)
    args = parser.parse_args(
        ["--upstream", "https://api.openai.com", "--max-retries", "0", "--insecure"],
    )
    c = HiveMindConfig()
    apply_serve_cli_args_to_config(c, args)
    assert c.upstream_url == "https://api.openai.com"
    assert c.max_retries == 0
    assert c.http_tls_verify is False


def test_proxy_cli_agent_limit_overrides():
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    args = parser.parse_args(
        ["--agent-limit", "batch-bot:rpm=20,tpm=40000", "--agent-limit", "interactive:rpm=50"],
    )
    c = hivemind_config_from_proxy_cli_args(args)
    assert c.agent_limit_overrides == {
        "batch-bot": {"rpm": 20, "tpm": 40000},
        "interactive": {"rpm": 50},
    }


def test_serve_cli_agent_limit_overrides():
    parser = argparse.ArgumentParser()
    register_serve_cli_arguments(parser)
    args = parser.parse_args(["--agent-limit", "bot:rpm=5"])
    c = HiveMindConfig()
    apply_serve_cli_args_to_config(c, args)
    assert c.agent_limit_overrides == {"bot": {"rpm": 5}}


def test_agent_limit_malformed_specs_raise():
    for bad in ("no-colon", ":rpm=5", "bot:", "bot:qps=5", "bot:rpm=abc", "bot:rpm=0", "bot:rpm"):
        with pytest.raises(ValueError):
            parse_agent_limit_specs([bad])


def test_config_validates_agent_limit_overrides():
    with pytest.raises(ValueError):
        HiveMindConfig(agent_limit_overrides={"a": {"rpm": 0}})
    cfg = HiveMindConfig(agent_limit_overrides={"a": {"rpm": 10}})
    assert cfg.to_dict()["agent_limit_overrides"] == {"a": {"rpm": 10}}
