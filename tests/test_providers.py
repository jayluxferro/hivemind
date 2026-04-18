"""Tests for provider profiles."""

from hivemind.scheduler.providers import (
    ANTHROPIC,
    OLLAMA,
    OPENAI,
    ProviderType,
    detect_provider,
    get_profile,
    list_providers,
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


def test_anthropic_profile_defaults():
    assert ANTHROPIC.default_max_concurrent == 5
    assert ANTHROPIC.auth_header == "x-api-key"
    assert "anthropic-ratelimit-requests-remaining" in ANTHROPIC.rate_limit_headers.values()


def test_openai_profile_defaults():
    assert OPENAI.default_max_concurrent == 10
    assert OPENAI.auth_header == "authorization"


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
