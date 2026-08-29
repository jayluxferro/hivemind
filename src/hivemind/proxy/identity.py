"""Resolve a per-request agent identity for rate limiting and budgets.

Two channels, deliberately separate:

* ``resolve_agent_id`` — the EXPLICIT identity (header or query param),
  or None when the client didn't identify itself. Used for budgets, DB
  logging, and metrics so unidentified traffic keeps its long-standing
  "untracked" behavior.
* ``resolve_rate_key`` — the bucket key for per-agent rate limiting.
  Resolution order: explicit identity → session hint extracted from the
  request body → credential+user-agent fingerprint → a single shared
  "anonymous" bucket as the last resort.

The session hint comes from Anthropic's ``metadata.user_id`` request field:
Claude Code populates it with a value embedding a per-session UUID
(``user_<acct>_account__session_<uuid>``), so two same-key sessions of the
same tool still land in distinct buckets — zero client configuration needed.
It is a heuristic, not a contract: if the field is absent, malformed, or the
tool doesn't send it, resolution falls through to the fingerprint unchanged.
The hint is hashed (namespaced by credential) — the raw value is never
stored or logged.

Fingerprinting likewise hashes the credential — the raw API key is never
stored or logged. Residual limitation: tools that neither set an explicit
header NOR send ``metadata.user_id`` still share one bucket per
credential+user-agent pair. For guaranteed session-level separation, clients
should send an explicit header (for Claude Code:
``ANTHROPIC_CUSTOM_HEADERS="x-hivemind-session-id: my-session"``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

# Bucket shared by requests with no explicit identity and no usable
# credential/user-agent to fingerprint.
ANONYMOUS_AGENT_ID = "anonymous"

# Bound identity length so a hostile/buggy client can't grow the limiter's
# per-agent dict with unbounded distinct keys.
_MAX_ID_LENGTH = 128

# Bound the body session hint before hashing — it is client-controlled.
_MAX_HINT_LENGTH = 512


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


def resolve_session_hint(body: bytes | None) -> str | None:
    """Per-session identifier embedded in the request body, if any.

    Reads Anthropic's ``metadata.user_id`` — Claude Code stamps a value that
    embeds a per-session UUID, so same-key sessions become distinguishable.
    Returns the raw (stripped, length-capped) value for the caller to hash;
    any parse failure or absent field returns None and resolution falls
    through to fingerprinting.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str):
        return None
    user_id = user_id.strip()
    return user_id[:_MAX_HINT_LENGTH] if user_id else None


def resolve_rate_key(
    headers: Mapping[str, str],
    query_params: Mapping[str, str],
    body: bytes | None = None,
) -> str:
    """Rate-limiter bucket key: explicit → session hint → fingerprint → anonymous."""
    explicit = resolve_agent_id(headers, query_params)
    if explicit:
        return explicit
    credential = headers.get("authorization") or headers.get("x-api-key")
    user_agent = headers.get("user-agent") or ""
    hint = resolve_session_hint(body)
    if hint:
        # Namespaced by credential so identical user_id strings on different
        # API keys can never share a bucket.
        digest = hashlib.sha256(f"{credential or ''}|{hint}".encode()).hexdigest()[:16]
        return f"mu-{digest}"
    if credential:
        digest = hashlib.sha256(f"{credential}|{user_agent}".encode()).hexdigest()[:16]
        return f"fp-{digest}"
    if user_agent:
        digest = hashlib.sha256(user_agent.encode()).hexdigest()[:16]
        return f"ua-{digest}"
    return ANONYMOUS_AGENT_ID
