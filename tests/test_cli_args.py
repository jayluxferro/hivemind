"""Tests for `hivemind.cli_args` (shared CLI for proxy and serve)."""

import argparse

from hivemind.cli_args import (
    apply_serve_cli_args_to_config,
    hivemind_config_from_proxy_cli_args,
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
