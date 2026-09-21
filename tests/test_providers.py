"""Tests for provider profiles."""

import pytest

from hivemind.scheduler.providers import (
    ANTHROPIC,
    AZURE_OPENAI,
    GENERIC,
    GOOGLE,
    OLLAMA,
    OPENAI,
    ProviderType,
    detect_provider,
    get_profile,
    list_providers,
    resolve_provider_profile,
)


def test_detect_anthropic():
    p = detect_provider("https://api.anthropic.com")
    assert p.provider_type == ProviderType.ANTHROPIC


def test_detect_openai():
    p = detect_provider("https://api.openai.com/v1")
    assert p.provider_type == ProviderType.OPENAI


def test_detect_ollama():
    p = detect_provider("http://localhost:11434")
    assert p.provider_type == ProviderType.OLLAMA

    p = detect_provider("http://127.0.0.1:11434")
    assert p.provider_type == ProviderType.OLLAMA


def test_detect_azure():
    p = detect_provider("https://myinstance.openai.azure.com/openai")
    assert p.provider_type == ProviderType.AZURE_OPENAI


def test_detect_google():
    p = detect_provider("https://generativelanguage.googleapis.com/v1beta")
    assert p.provider_type == ProviderType.GOOGLE


def test_detect_unknown_returns_generic():
    p = detect_provider("https://my-custom-api.example.com")
    assert p.provider_type == ProviderType.GENERIC


def test_get_profile_by_string():
    p = get_profile("anthropic")
    assert p.provider_type == ProviderType.ANTHROPIC


def test_get_profile_by_enum():
    p = get_profile(ProviderType.OPENAI)
    assert p.name == "OpenAI"


def test_get_profile_invalid_string_falls_back_to_generic():
    p = get_profile("not-a-real-provider")
    assert p is GENERIC


def test_get_profile_case_and_whitespace_insensitive():
    assert get_profile("ANTHROPIC").provider_type == ProviderType.ANTHROPIC
    assert get_profile("  openai  ").provider_type == ProviderType.OPENAI


def test_hivemind_config_normalize_runtime_limits():
    from hivemind.storage.models import HiveMindConfig

    c = HiveMindConfig(max_concurrency=-3, min_concurrency=50)
    c.normalize_runtime_limits()
    assert c.max_concurrency == 1
    assert c.min_concurrency == 1


def test_apply_provider_defaults_normalizes_extreme_concurrency():
    from hivemind.storage.models import HiveMindConfig

    c = HiveMindConfig(
        max_concurrency=0,
        min_concurrency=99,
        upstream_url="https://api.anthropic.com",
    )
    c.apply_provider_defaults()
    assert c.max_concurrency == 1
    assert c.min_concurrency == 1


def test_anthropic_profile_defaults():
    assert ANTHROPIC.default_max_concurrent == 5
    assert ANTHROPIC.auth_header == "x-api-key"
    assert "anthropic-ratelimit-requests-remaining" in ANTHROPIC.rate_limit_headers.values()


def test_openai_profile_defaults():
    assert OPENAI.default_max_concurrent == 10
    assert OPENAI.auth_header == "authorization"


def test_input_shape_flags_split_the_contracts():
    """The ledger's fresh-only ingest normalization rides this flag: True
    means reported input INCLUDES cached tokens (OpenAI contract —
    prompt_tokens counts the cached subset inside itself), False means
    FRESH-only (Anthropic contract, incl. DeepSeek's shim, whose
    input_tokens excludes cache_read_input_tokens).  The ANTHROPIC profile
    is the only documented fresh-shape contract hivemind detects; everything
    else is OpenAI-compat by shape or by convention."""
    assert ANTHROPIC.input_includes_cached is False
    for profile in (OPENAI, AZURE_OPENAI, GOOGLE, OLLAMA, GENERIC):
        assert profile.input_includes_cached is True, profile.name


def test_ollama_profile_high_limits():
    # Ollama is local — effectively unlimited rate
    assert OLLAMA.default_requests_per_minute >= 1000
    assert OLLAMA.default_max_concurrent == 2  # GPU limited


def test_list_providers():
    providers = list_providers()
    assert len(providers) >= 5
    names = [p["name"] for p in providers]
    assert "Anthropic" in names
    assert "OpenAI" in names
    assert "Ollama (local)" in names


def test_profile_to_dict():
    d = ANTHROPIC.to_dict()
    assert d["provider_type"] == "anthropic"
    assert "default_requests_per_minute" in d


# --- dual-shape hosts: the URL path picks the contract ------------------------
#
# DeepSeek and Z.AI each serve BOTH wire shapes under one domain (Anthropic
# shim at */anthropic, OpenAI-compat everywhere else).  A flat domain match
# flagged every one of those URLs fresh-only, so a config pointed at the
# OpenAI-shape endpoint silently reintroduced the double-billing bug commit
# 2cf6e4d fixed — the ingest normalization never ran, and the ledger clamp
# only guards True profiles, so nothing else would catch it either.


def test_deepseek_anthropic_shim_detects_fresh_shape():
    p = detect_provider("https://api.deepseek.com/anthropic")
    assert p.provider_type == ProviderType.ANTHROPIC
    assert p.input_includes_cached is False


def test_deepseek_openai_shape_detects_total_shape():
    """The footgun: /v1 and the bare domain are DeepSeek's DOCUMENTED OpenAI
    base — prompt_tokens INCLUDES cached_tokens there.  Detection must say
    OPENAI (True) so the ledger normalization runs; ANTHROPIC here is what
    double-billed every cached token."""
    for url in ("https://api.deepseek.com/v1", "https://api.deepseek.com"):
        p = detect_provider(url)
        assert p.provider_type == ProviderType.OPENAI, url
        assert p.input_includes_cached is True, url


def test_zai_path_decides_the_shape():
    """Z.AI hosts the Anthropic shim at /api/anthropic and its OpenAI-compat
    API at /api/paas/v4 — same domain, opposite contracts."""
    p = detect_provider("https://api.z.ai/api/anthropic")
    assert p.provider_type == ProviderType.ANTHROPIC
    assert p.input_includes_cached is False

    p = detect_provider("https://api.z.ai/api/paas/v4")
    assert p.provider_type == ProviderType.OPENAI
    assert p.input_includes_cached is True


# Every fallback_upstream hivemind can inherit across the production
# manifold*.yaml set (hivemind is the LAST pipeline service in each, so the
# gateway's fallback_upstream IS what the interceptor detects on).  These
# pins are the no-regression contract for the path-aware detection change:
# each URL must keep detecting exactly the same shape as before it.
@pytest.mark.parametrize(
    ("upstream", "expected_type", "expected_flag", "manifold_config"),
    [
        ("https://api.deepseek.com/anthropic", ProviderType.ANTHROPIC, False, "manifold.yaml"),
        ("https://api.z.ai/api/anthropic", ProviderType.ANTHROPIC, False, "manifold-zai.yaml"),
        ("https://api.myapi.world", ProviderType.ANTHROPIC, False, "manifold-myapi-world.yaml"),
        ("https://api.kimi.com/coding/", ProviderType.ANTHROPIC, False, "manifold-anthropic-kimi.yaml"),
        ("https://api.anthropic.com", ProviderType.ANTHROPIC, False, "manifold-anthropic-new.yaml"),
        ("https://api.doubleword.ai", ProviderType.OPENAI, True, "manifold-doublewordai.yaml"),
    ],
)
def test_production_manifold_endpoints_detect_unchanged(upstream, expected_type, expected_flag, manifold_config):
    p = detect_provider(upstream)
    assert p.provider_type == expected_type, manifold_config
    assert p.input_includes_cached is expected_flag, manifold_config


# --- the operator's usage-shape escape hatch ---------------------------------


def test_resolve_provider_profile_default_follows_detection():
    """No override (None) returns the detected singleton itself."""
    assert resolve_provider_profile("https://api.anthropic.com", None) is ANTHROPIC
    assert resolve_provider_profile("https://api.openai.com", None) is OPENAI


def test_resolve_provider_profile_forces_flag_against_the_profile():
    """An explicit True/False wins over whatever detection derived — the
    unlisted-Anthropic-shape-gateway victim detects GENERIC/True and needs
    False; an OpenAI-shape endpoint detected as fresh-only needs True."""
    p = resolve_provider_profile("https://api.anthropic.com", True)
    assert p is not ANTHROPIC  # singletons are shared — never mutate them
    assert p.name == "Anthropic"
    assert p.provider_type == ProviderType.ANTHROPIC
    assert p.input_includes_cached is True
    assert ANTHROPIC.input_includes_cached is False  # the singleton is untouched

    p = resolve_provider_profile("https://gw.example.com", False)
    assert p.provider_type == ProviderType.GENERIC
    assert p.input_includes_cached is False
    assert GENERIC.input_includes_cached is True  # singleton untouched


def test_resolve_provider_profile_matching_override_returns_singleton():
    """Forcing the value the profile already has needs no copy."""
    assert resolve_provider_profile("https://api.anthropic.com", False) is ANTHROPIC
    assert resolve_provider_profile("https://api.openai.com", True) is OPENAI
