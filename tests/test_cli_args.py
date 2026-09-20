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


def test_max_rate_wait_flag_on_both_parsers():
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    args = parser.parse_args(["--max-rate-wait", "30"])
    c = hivemind_config_from_proxy_cli_args(args)
    assert c.max_rate_wait_s == 30.0

    parser2 = argparse.ArgumentParser()
    register_serve_cli_arguments(parser2)
    args2 = parser2.parse_args(["--max-rate-wait", "15.5"])
    c2 = HiveMindConfig()
    apply_serve_cli_args_to_config(c2, args2)
    assert c2.max_rate_wait_s == 15.5


def test_max_rate_wait_default_and_validation():
    assert HiveMindConfig().max_rate_wait_s == 240.0
    with pytest.raises(ValueError):
        HiveMindConfig(max_rate_wait_s=0)
    with pytest.raises(ValueError):
        HiveMindConfig(max_rate_wait_s=-1)


def test_proxy_cli_max_rate_wait_zero_fails_at_config_build():
    """The proxy path must validate --max-rate-wait at apply time, like the
    serve path does: `--max-rate-wait 0` used to pass config-build silently
    and only die later at rate-limiter build (the serve path applied the
    same normalize call immediately)."""
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    for bad in ("0", "-1"):
        args = parser.parse_args(["--max-rate-wait", bad])
        with pytest.raises(ValueError):
            hivemind_config_from_proxy_cli_args(args)


def test_serve_cli_max_rate_wait_zero_fails_at_apply():
    """Pins the serve-path behavior the proxy path now matches."""
    parser = argparse.ArgumentParser()
    register_serve_cli_arguments(parser)
    args = parser.parse_args(["--max-rate-wait", "0"])
    with pytest.raises(ValueError):
        apply_serve_cli_args_to_config(HiveMindConfig(), args)


# --- token ledger DSN (SPEC-token-ledger §5) ----------------------------------


def _proxy_config(argv):
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    args = parser.parse_args(argv)
    return hivemind_config_from_proxy_cli_args(args)


def test_proxy_cli_telemetry_dsn_flag(monkeypatch):
    monkeypatch.delenv("MESH_TELEMETRY_DSN", raising=False)
    c = _proxy_config(["--telemetry-dsn", "postgresql://user:pass@db:5432/mesh"])
    assert c.telemetry_dsn == "postgresql://user:pass@db:5432/mesh"
    assert c.to_dict()["telemetry_dsn"] == "postgresql://user:pass@db:5432/mesh"


def test_proxy_cli_telemetry_dsn_env_fallback(monkeypatch):
    monkeypatch.setenv("MESH_TELEMETRY_DSN", "postgresql://env@localhost:5432/telemetry")
    c = _proxy_config([])  # no flag -> env fallback
    assert c.telemetry_dsn == "postgresql://env@localhost:5432/telemetry"


def test_proxy_cli_telemetry_dsn_flag_beats_env(monkeypatch):
    monkeypatch.setenv("MESH_TELEMETRY_DSN", "postgresql://env@localhost:5432/telemetry")
    c = _proxy_config(["--telemetry-dsn", "postgresql://flag@localhost:5432/telemetry"])
    assert c.telemetry_dsn == "postgresql://flag@localhost:5432/telemetry"


def test_proxy_cli_telemetry_dsn_unset_defaults_to_request_log_db(monkeypatch):
    monkeypatch.delenv("MESH_TELEMETRY_DSN", raising=False)
    c = _proxy_config([])
    assert c.telemetry_dsn == "postgresql://hivemind@localhost:5432/hivemind"
    assert c.to_dict()["telemetry_dsn"] == "postgresql://hivemind@localhost:5432/hivemind"


# --- token ledger retention (SPEC-analytics D10) ------------------------------


def test_telemetry_retention_days_flag_on_both_parsers():
    c = _proxy_config(["--telemetry-retention-days", "30"])
    assert c.telemetry_retention_days == 30

    parser = argparse.ArgumentParser()
    register_serve_cli_arguments(parser)
    args = parser.parse_args(["--telemetry-retention-days", "7"])
    c2 = HiveMindConfig()
    apply_serve_cli_args_to_config(c2, args)
    assert c2.telemetry_retention_days == 7


def test_telemetry_retention_days_defaults_and_env_fallback(monkeypatch):
    monkeypatch.delenv("HIVEMIND_TELEMETRY_RETENTION_DAYS", raising=False)
    c = _proxy_config([])
    assert c.telemetry_retention_days == 90
    assert c.to_dict()["telemetry_retention_days"] == 90

    monkeypatch.setenv("HIVEMIND_TELEMETRY_RETENTION_DAYS", "7")
    assert _proxy_config([]).telemetry_retention_days == 7  # no flag -> env


def test_telemetry_retention_days_flag_beats_env(monkeypatch):
    monkeypatch.setenv("HIVEMIND_TELEMETRY_RETENTION_DAYS", "7")
    assert _proxy_config(["--telemetry-retention-days", "365"]).telemetry_retention_days == 365


def test_telemetry_retention_days_invalid_env_is_fatal(monkeypatch):
    # Deliberately fail-loud: a typo'd env var must not silently become 90
    # (and silently keep months of rows).
    for bad in ("nope", "0", "-3", "1.5", ""):
        monkeypatch.setenv("HIVEMIND_TELEMETRY_RETENTION_DAYS", bad)
        if bad == "":
            assert HiveMindConfig().telemetry_retention_days == 90  # blank == unset
            continue
        with pytest.raises(ValueError):
            HiveMindConfig()
        with pytest.raises(ValueError):
            _proxy_config([])


def test_telemetry_retention_days_validation():
    assert HiveMindConfig(telemetry_retention_days=1).telemetry_retention_days == 1
    assert HiveMindConfig(telemetry_retention_days=365).telemetry_retention_days == 365
    for bad in (0, -1, True, "90", 1.5):
        with pytest.raises(ValueError):
            HiveMindConfig(telemetry_retention_days=bad)


def test_telemetry_retention_days_flag_revalidates(monkeypatch):
    # The CLI path re-runs validation, so a bad flag value is caught here too.
    with pytest.raises(ValueError):
        _proxy_config(["--telemetry-retention-days", "0"])
    with pytest.raises(SystemExit):
        _proxy_config(["--telemetry-retention-days", "not-a-number"])


# --- rate-limiting kill-switch ----------------------------------------------


def test_proxy_cli_no_rate_limiting_flag():
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    args = parser.parse_args(["--no-rate-limiting"])
    c = hivemind_config_from_proxy_cli_args(args)
    assert c.rate_limiting_enabled is False
    assert c.to_dict()["rate_limiting_enabled"] is False


def test_proxy_cli_rate_limiting_default_on():
    parser = argparse.ArgumentParser()
    register_proxy_cli_arguments(parser)
    c = hivemind_config_from_proxy_cli_args(parser.parse_args([]))
    assert c.rate_limiting_enabled is True


def test_serve_cli_no_rate_limiting_flag():
    parser = argparse.ArgumentParser()
    register_serve_cli_arguments(parser)
    args = parser.parse_args(["--no-rate-limiting"])
    c = HiveMindConfig()
    apply_serve_cli_args_to_config(c, args)
    assert c.rate_limiting_enabled is False
