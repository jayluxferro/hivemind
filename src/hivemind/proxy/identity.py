"""Resolve a per-request agent identity for rate limiting and budgets.

Two channels, deliberately separate:

* ``resolve_agent_id`` — the EXPLICIT identity (header or query param),
  or None when the client didn't identify itself. Used for budgets, DB
  logging, and metrics so unidentified traffic keeps its long-standing
  "untracked" behavior.
* ``resolve_rate_key`` — the bucket key for per-agent rate limiting.
  Falls back to a credential+user-agent fingerprint so distinct tools or
  API keys get distinct buckets even when they don't set headers, and to
  a single shared "anonymous" bucket as the last resort.

Fingerprinting hashes the credential — the raw API key is never stored or
logged. One honest limitation: two sessions of the SAME tool using the SAME
API key (e.g. two Claude Code instances on one account) produce the same
fingerprint and share a bucket. For session-level separation, clients should
send an explicit header (for Claude Code:
``ANTHROPIC_CUSTOM_HEADERS="x-hivemind-session-id: my-session"``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

# Bucket shared by requests with no explicit identity and no usable
# credential/user-agent to fingerprint.
ANONYMOUS_AGENT_ID = "anonymous"

# Bound identity length so a hostile/buggy client can't grow the limiter's
# per-agent dict with unbounded distinct keys.
_MAX_ID_LENGTH = 128


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value[:_MAX_ID_LENGTH] if value else None


def resolve_agent_id(
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
) -> str | None:
    """Explicit agent identity, or None if the client didn't provide one."""
    for name in ("x-hivemind-agent-id", "x-hivemind-session-id"):
        ident = _clean(headers.get(name))
        if ident:
            return ident
    return _clean(query_params.get("agent_id"))


def resolve_rate_key(
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
) -> str:
    """Rate-limiter bucket key: explicit identity → fingerprint → anonymous."""
    explicit = resolve_agent_id(headers, query_params)
    if explicit:
        return explicit
    credential = headers.get("authorization") or headers.get("x-api-key")
    user_agent = headers.get("user-agent") or ""
    if credential:
        digest = hashlib.sha256(f"{credential}|{user_agent}".encode()).hexdigest()[:16]
        return f"fp-{digest}"
    if user_agent:
        digest = hashlib.sha256(user_agent.encode()).hexdigest()[:16]
        return f"ua-{digest}"
    return ANONYMOUS_AGENT_ID
