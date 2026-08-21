"""Provider profiles — pre-configured rate limit behaviors for LLM API providers.

Each provider has different:
- Rate limit header formats
- Default rate limits
- Endpoint patterns
- Authentication schemes

Auto-detects provider from upstream URL or explicit configuration.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ProviderType(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"
    AZURE_OPENAI = "azure_openai"
    GOOGLE = "google"
    GENERIC = "generic"


@dataclass
class ProviderProfile:
    """Configuration profile for a specific LLM API provider."""

    provider_type: ProviderType
    name: str

    # Rate limits (defaults — actual limits come from headers at runtime)
    default_requests_per_minute: int = 60
    default_tokens_per_minute: int = 100_000
    default_max_concurrent: int = 5

    # Header format for parsing rate limit info
    rate_limit_headers: dict[str, str] = field(default_factory=dict)

    # Retry behavior
    retryable_status_codes: set[int] = field(default_factory=lambda: {429, 500, 502, 503, 529})
    default_retry_after_seconds: float = 2.0

    # Endpoint patterns
    messages_path: str = "/v1/messages"
    completions_path: str = "/v1/chat/completions"

    # Auth header name
    auth_header: str = "x-api-key"

    # Backpressure tuning
    latency_target_ms: float = 2000.0
    aimd_additive_increase: float = 0.5
    aimd_multiplicative_decrease: float = 0.5

    def to_dict(self) -> dict:
        return {
            "provider_type": self.provider_type.value,
            "name": self.name,
            "default_requests_per_minute": self.default_requests_per_minute,
            "default_tokens_per_minute": self.default_tokens_per_minute,
            "default_max_concurrent": self.default_max_concurrent,
            "latency_target_ms": self.latency_target_ms,
        }


# --- Provider Profiles ---

ANTHROPIC = ProviderProfile(
    provider_type=ProviderType.ANTHROPIC,
    name="Anthropic",
    default_requests_per_minute=50,
    default_tokens_per_minute=80_000,
    default_max_concurrent=5,
    rate_limit_headers={
        "remaining_requests": "anthropic-ratelimit-requests-remaining",
        "remaining_tokens": "anthropic-ratelimit-tokens-remaining",
        "limit_requests": "anthropic-ratelimit-requests-limit",
        "limit_tokens": "anthropic-ratelimit-tokens-limit",
        "reset_requests": "anthropic-ratelimit-requests-reset",
        "reset_tokens": "anthropic-ratelimit-tokens-reset",
    },
    retryable_status_codes={429, 500, 502, 503, 529},
    default_retry_after_seconds=2.0,
    messages_path="/v1/messages",
    auth_header="x-api-key",
    latency_target_ms=3000.0,
    aimd_additive_increase=0.5,
    aimd_multiplicative_decrease=0.5,
)

OPENAI = ProviderProfile(
    provider_type=ProviderType.OPENAI,
    name="OpenAI",
    default_requests_per_minute=60,
    default_tokens_per_minute=150_000,
    default_max_concurrent=10,
    rate_limit_headers={
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "limit_requests": "x-ratelimit-limit-requests",
        "limit_tokens": "x-ratelimit-limit-tokens",
    },
    retryable_status_codes={429, 500, 502, 503},
    default_retry_after_seconds=1.0,
    completions_path="/v1/chat/completions",
    auth_header="authorization",
    latency_target_ms=2000.0,
    aimd_additive_increase=1.0,
    aimd_multiplicative_decrease=0.5,
)

OLLAMA = ProviderProfile(
    provider_type=ProviderType.OLLAMA,
    name="Ollama (local)",
    default_requests_per_minute=1000,  # Local — effectively unlimited
    default_tokens_per_minute=10_000_000,
    default_max_concurrent=2,  # But limited by GPU memory
    rate_limit_headers={},  # Ollama doesn't send rate limit headers
    retryable_status_codes={500, 502, 503},
    default_retry_after_seconds=0.5,
    completions_path="/api/chat",
    auth_header="",
    latency_target_ms=10000.0,  # Local models are slower
    aimd_additive_increase=0.5,
    aimd_multiplicative_decrease=0.7,
)

AZURE_OPENAI = ProviderProfile(
    provider_type=ProviderType.AZURE_OPENAI,
    name="Azure OpenAI",
    default_requests_per_minute=60,
    default_tokens_per_minute=120_000,
    default_max_concurrent=10,
    rate_limit_headers={
        "remaining_requests": "x-ratelimit-remaining-requests",
        "remaining_tokens": "x-ratelimit-remaining-tokens",
        "limit_requests": "x-ratelimit-limit-requests",
        "limit_tokens": "x-ratelimit-limit-tokens",
    },
    retryable_status_codes={429, 500, 502, 503},
    default_retry_after_seconds=2.0,
    auth_header="api-key",
    latency_target_ms=3000.0,
)

GOOGLE = ProviderProfile(
    provider_type=ProviderType.GOOGLE,
    name="Google AI (Gemini)",
    default_requests_per_minute=60,
    default_tokens_per_minute=100_000,
    default_max_concurrent=8,
    rate_limit_headers={},
    retryable_status_codes={429, 500, 503},
    default_retry_after_seconds=2.0,
    completions_path="/v1beta/models",
    auth_header="x-goog-api-key",
    latency_target_ms=2000.0,
)

GENERIC = ProviderProfile(
    provider_type=ProviderType.GENERIC,
    name="Generic",
    default_requests_per_minute=60,
    default_tokens_per_minute=100_000,
    default_max_concurrent=5,
)

# Registry
_PROFILES: dict[ProviderType, ProviderProfile] = {
    ProviderType.ANTHROPIC: ANTHROPIC,
    ProviderType.OPENAI: OPENAI,
    ProviderType.OLLAMA: OLLAMA,
    ProviderType.AZURE_OPENAI: AZURE_OPENAI,
    ProviderType.GOOGLE: GOOGLE,
    ProviderType.GENERIC: GENERIC,
}

# URL patterns for auto-detection
_URL_PATTERNS: list[tuple[str, ProviderType]] = [
    (r"api\.anthropic\.com", ProviderType.ANTHROPIC),
    (r"api\.deepseek\.com", ProviderType.ANTHROPIC),
    (r"api\.myapi\.world", ProviderType.ANTHROPIC),
    (r"api\.kimi\.com", ProviderType.ANTHROPIC),
    (r"api\.z\.ai", ProviderType.ANTHROPIC),
    (r"api\.openai\.com", ProviderType.OPENAI),
    (r"api\.doubleword\.ai", ProviderType.OPENAI),
    (r"openai\.azure\.com", ProviderType.AZURE_OPENAI),
    (r"generativelanguage\.googleapis\.com", ProviderType.GOOGLE),
    (r"localhost:11434", ProviderType.OLLAMA),
    (r"127\.0\.0\.1:11434", ProviderType.OLLAMA),
]


def detect_provider(upstream_url: str) -> ProviderProfile:
    """Auto-detect provider from upstream URL."""
    for pattern, provider_type in _URL_PATTERNS:
        if re.search(pattern, upstream_url):
            return _PROFILES[provider_type]
    return _PROFILES[ProviderType.GENERIC]


def get_profile(provider_type: ProviderType | str) -> ProviderProfile:
    """Get a provider profile by type."""
    if isinstance(provider_type, str):
        raw = provider_type
        candidates: list[str] = []
        for c in (raw, raw.strip(), raw.strip().lower()):
            if c and c not in candidates:
                candidates.append(c)
        for cand in candidates:
            try:
                provider_type = ProviderType(cand)
                break
            except ValueError:
                continue
        else:
            logger.warning("Unknown provider type %r, using GENERIC", raw)
            return GENERIC
    return _PROFILES.get(provider_type, GENERIC)


def list_providers() -> list[dict]:
    """List all available provider profiles."""
    return [p.to_dict() for p in _PROFILES.values()]
