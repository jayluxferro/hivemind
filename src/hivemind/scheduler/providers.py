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
from dataclasses import dataclass, field, replace
from enum import Enum
from urllib.parse import urlsplit

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

    # Usage-shape flag for the token ledger's fresh-only billing invariant
    # (telemetry/ledger.py): True when the provider's reported input count
    # INCLUDES cached tokens — the OpenAI contract, where usage.prompt_tokens
    # counts every input token and usage.prompt_tokens_details.cached_tokens
    # names the cached subset of it.  False when reported input is FRESH-only
    # — the Anthropic contract (incl. DeepSeek's Anthropic-compatible shim),
    # where usage.input_tokens excludes cache_read_input_tokens.  Profiles
    # with True get their ledger rows normalized at record time
    # (tokens_in = reported_total - cache_read); without this, cached tokens
    # are priced twice (once at price_in, once at price_cache_read).
    input_includes_cached: bool = False

    def to_dict(self) -> dict:
        return {
            "provider_type": self.provider_type.value,
            "name": self.name,
            "default_requests_per_minute": self.default_requests_per_minute,
            "default_tokens_per_minute": self.default_tokens_per_minute,
            "default_max_concurrent": self.default_max_concurrent,
            "latency_target_ms": self.latency_target_ms,
            "input_includes_cached": self.input_includes_cached,
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
    input_includes_cached=True,  # prompt_tokens includes cached_tokens
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
    # OpenAI-compatible response shape; the flag is moot in practice (Ollama
    # does no prompt caching, so no cached_tokens ever arrives) but the shape
    # is what it responds with.
    input_includes_cached=True,
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
    input_includes_cached=True,  # Azure OpenAI speaks the OpenAI usage contract
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
    # Gemini's OpenAI-compat layer reports prompt_tokens INCLUDING the cached
    # subset (prompt_tokens_details.cached_tokens), not a fresh-only count.
    input_includes_cached=True,
)

GENERIC = ProviderProfile(
    provider_type=ProviderType.GENERIC,
    name="Generic",
    default_requests_per_minute=60,
    default_tokens_per_minute=100_000,
    default_max_concurrent=5,
    # Unknown upstream: assume the OpenAI-compatible norm (vLLM, LiteLLM,
    # OpenRouter and friends all report prompt_tokens including cached).
    # The flag only bites when the response actually carries cache fields —
    # a generic upstream that reports cached_tokens is speaking the OpenAI
    # contract, where those tokens sit INSIDE prompt_tokens.  The known
    # Anthropic-shape exception is exactly what detect_provider maps onto
    # the ANTHROPIC profile (False).
    input_includes_cached=True,
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

# URL patterns for auto-detection.  Domains listed in _DUAL_SHAPE_HOSTS
# below are deliberately NOT here: their shape depends on the URL path, so
# a flat domain match cannot decide it.
_URL_PATTERNS: list[tuple[str, ProviderType]] = [
    (r"api\.anthropic\.com", ProviderType.ANTHROPIC),
    (r"api\.myapi\.world", ProviderType.ANTHROPIC),
    (r"api\.kimi\.com", ProviderType.ANTHROPIC),
    (r"api\.openai\.com", ProviderType.OPENAI),
    (r"api\.doubleword\.ai", ProviderType.OPENAI),
    (r"openai\.azure\.com", ProviderType.AZURE_OPENAI),
    (r"generativelanguage\.googleapis\.com", ProviderType.GOOGLE),
    (r"localhost:11434", ProviderType.OLLAMA),
    (r"127\.0\.0\.1:11434", ProviderType.OLLAMA),
]

# Domains that host BOTH wire shapes under one host — the URL path picks
# the contract, so detection must be path-aware for them.  DeepSeek serves
# its Anthropic-compatible shim at /anthropic and its OpenAI-compatible
# API at the bare domain and /v1; Z.AI serves the shim at /api/anthropic
# and its OpenAI-compatible API at /api/paas/v4.  A flat domain→ANTHROPIC
# match silently mis-flagged the OpenAI-shape endpoints as fresh-only,
# which is exactly the double-billing bug the ledger normalization fixed
# (commit 2cf6e4d) — the flag suppressed the subtraction, and the clamp
# only runs for True profiles, so nothing else would catch it either.
_DUAL_SHAPE_HOSTS: tuple[str, ...] = (
    r"api\.deepseek\.com",
    r"api\.z\.ai",
)


def _dual_shape_profile(upstream_url: str) -> ProviderProfile:
    """Resolve a dual-shape host by URL path.

    ``/anthropic`` anywhere in the path is the provider's Anthropic shim
    (fresh-only input).  Everything else — bare domain, ``/v1``, any other
    prefix — speaks the OpenAI contract (``prompt_tokens`` INCLUDES the
    cached subset), which is those hosts' documented default.
    """
    try:
        path = urlsplit(upstream_url).path or ""
    except ValueError:  # unparseable URL — judge it by its literal text
        path = upstream_url
    if "/anthropic" in path:
        return _PROFILES[ProviderType.ANTHROPIC]
    return _PROFILES[ProviderType.OPENAI]


def detect_provider(upstream_url: str) -> ProviderProfile:
    """Auto-detect provider from upstream URL."""
    for pattern, provider_type in _URL_PATTERNS:
        if re.search(pattern, upstream_url):
            return _PROFILES[provider_type]
    for pattern in _DUAL_SHAPE_HOSTS:
        if re.search(pattern, upstream_url):
            return _dual_shape_profile(upstream_url)
    return _PROFILES[ProviderType.GENERIC]


def resolve_provider_profile(
    upstream_url: str,
    input_includes_cached: bool | None = None,
) -> ProviderProfile:
    """detect_provider plus the operator's usage-shape escape hatch.

    The ledger's fresh-only ingest normalization keys off the profile's
    ``input_includes_cached`` flag, which detection derives from the URL.
    Detection can be wrong for an unlisted gateway (an Anthropic-shape
    proxy that detects GENERIC/True clamps every cached request to
    tokens_in=0), so the operator can force the shape explicitly:
    ``--input-includes-cached`` / ``--input-excludes-cached`` land here as
    True/False; None keeps the profile-derived default.

    Returns a copy when overriding — profiles are shared singletons and
    must never be mutated.  The copy shares the singleton's rate-limit
    header tables (read-only by convention, as on the singleton itself).
    """
    profile = detect_provider(upstream_url)
    if input_includes_cached is None or input_includes_cached == profile.input_includes_cached:
        return profile
    return replace(profile, input_includes_cached=input_includes_cached)


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
