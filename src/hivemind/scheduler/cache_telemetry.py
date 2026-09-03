"""Passive prompt-cache telemetry derived from real traffic.

Providers that support prompt caching report it in response usage fields
(Anthropic-style: ``cache_creation_input_tokens`` / ``cache_read_input_tokens``;
OpenAI-style: ``usage.prompt_tokens_details.cached_tokens``). Watching real
traffic costs nothing, needs no probe spend, and works for every upstream
manifold chains — cache support is discovered the moment a client actually
uses it. The ``/_probe/cache`` endpoint complements this with an explicit,
once-per-provider active check.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_ANTHROPIC_CACHE_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")


def _cache_fields_from_usage(usage: dict) -> dict[str, int]:
    """Pull positive cache-related numbers out of a usage dict."""
    out: dict[str, int] = {}
    if not isinstance(usage, dict):
        return out
    for key in _ANTHROPIC_CACHE_KEYS:
        value = usage.get(key)
        if isinstance(value, int) and value > 0:
            out[key] = value
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        value = details.get("cached_tokens")
        if isinstance(value, int) and value > 0:
            out["cached_tokens"] = value
    return out


def _cache_usage_from_payload(data) -> dict[str, int]:
    """Cache fields from one decoded JSON payload (empty if absent).

    Handles both response shapes: non-streaming bodies (``usage`` at the top
    level) and streaming events, where the full usage block rides nested
    inside ``message_start`` → ``message`` → ``usage``.
    """
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage")
    if not isinstance(usage, dict):
        message = data.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    return _cache_fields_from_usage(usage)


def extract_cache_usage(body: bytes) -> dict[str, int]:
    """Pull cache-related usage numbers from a non-streaming response body.

    ``json.loads`` needs the WHOLE body to be one JSON document, so this is
    only valid for buffered bodies — see ``extract_cache_usage_from_sse`` for
    SSE byte chunks.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return _cache_usage_from_payload(data)


def extract_cache_usage_from_sse(chunk: bytes) -> dict[str, int]:
    """Cache fields from an SSE byte chunk (multi-line event/data frames).

    Streaming chunks are not one JSON document — they carry ``event:`` and
    ``data:`` lines, often several payloads, occasionally a ``[DONE]`` marker
    or garbage from a dying upstream.  This walks every ``data:`` line
    individually and merges per-key MAXIMA across payloads: Anthropic usage
    blocks are cumulative snapshots (``message_start`` then ``message_delta``),
    so the max is the snapshot that wins.  Best-effort: a payload split across
    a chunk boundary is skipped rather than misread.
    """
    merged: dict[str, int] = {}
    try:
        lines = chunk.decode("utf-8", errors="replace").splitlines()
    except Exception:  # pragma: no cover - decode with errors=replace cannot raise
        return merged
    for line in lines:
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            fields = _cache_usage_from_payload(json.loads(payload))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for key, value in fields.items():
            merged[key] = max(merged.get(key, 0), value)
    return merged


def request_uses_cache_control(body: bytes) -> bool:
    """True if the request body carries any ``cache_control`` markers."""
    return b'"cache_control"' in body


@dataclass
class CacheTelemetry:
    """Aggregated cache signal for one upstream provider."""

    requests_seen: int = 0
    requests_with_cache_control: int = 0
    responses_with_cache_usage: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def observe_request(self, body: bytes) -> None:
        self.requests_seen += 1
        if request_uses_cache_control(body):
            self.requests_with_cache_control += 1

    def observe_response(self, body: bytes) -> None:
        fields = extract_cache_usage(body)
        if not fields:
            return
        self._record(fields)

    def record_probe(self, usage: dict) -> bool:
        """Record an explicit probe result; True if the provider honored caching."""
        fields = _cache_fields_from_usage(usage)
        if fields:
            self._record(fields)
        return bool(fields)

    def _record(self, fields: dict[str, int]) -> None:
        self.responses_with_cache_usage += 1
        self.cache_creation_tokens += fields.get("cache_creation_input_tokens", 0)
        self.cache_read_tokens += fields.get("cache_read_input_tokens", 0) + fields.get("cached_tokens", 0)

    @property
    def cache_supported(self) -> bool:
        """True once the provider has honored caching on at least one response."""
        return self.cache_read_tokens > 0 or self.cache_creation_tokens > 0

    def snapshot(self) -> dict:
        return {
            "requests_seen": self.requests_seen,
            "requests_with_cache_control": self.requests_with_cache_control,
            "responses_with_cache_usage": self.responses_with_cache_usage,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_supported": self.cache_supported,
        }
