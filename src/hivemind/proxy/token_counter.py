"""Token counter — extracts token usage from API request/response bodies.

Supports Anthropic and OpenAI response formats. For requests, estimates
tokens from message content length as a rough proxy when actual counts
aren't available.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Rough estimate: 1 token ≈ 4 characters for English text
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Rough token estimate from character count."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_request_tokens(body: bytes) -> int:
    """Estimate input tokens from an API request body."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return estimate_tokens(body.decode("utf-8", errors="replace"))

    total = 0

    # System prompt
    if "system" in data:
        if isinstance(data["system"], str):
            total += estimate_tokens(data["system"])
        elif isinstance(data["system"], list):
            for block in data["system"]:
                if isinstance(block, dict) and "text" in block:
                    total += estimate_tokens(block["text"])

    # Messages
    for msg in data.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if "text" in block:
                        total += estimate_tokens(block["text"])
                    elif "source" in block:
                        # Image blocks — rough estimate
                        total += 1000

    # Tools definitions
    for tool in data.get("tools", []):
        total += estimate_tokens(json.dumps(tool))

    return total


def count_response_tokens(body: bytes) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from an API response body.

    Returns actual usage counts from the response when available,
    falls back to estimation.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        est = estimate_tokens(body.decode("utf-8", errors="replace"))
        return 0, est

    usage = data.get("usage", {})

    # Anthropic format
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    # OpenAI format
    if not input_tokens and not output_tokens:
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)

    # If no usage info, estimate from content
    if not output_tokens:
        # Try to estimate from response content
        content = data.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    output_tokens += estimate_tokens(block["text"])
        # OpenAI format
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            if "content" in msg and msg["content"]:
                output_tokens += estimate_tokens(msg["content"])

    return input_tokens, output_tokens


def count_streaming_tokens(chunk: bytes) -> tuple[int, int]:
    """Extract token counts from a streaming chunk (SSE event).

    Most chunks don't have usage info — only the final message_stop/done event does.
    Returns (0, 0) for chunks without usage data.
    """
    try:
        # SSE format: "data: {...}\n\n" or "event: ...\ndata: {...}\n\n"
        text = chunk.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                payload = line[6:]
                if payload == "[DONE]":
                    return 0, 0
                data = json.loads(payload)
                usage = data.get("usage", {})
                if usage:
                    input_t = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                    output_t = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                    return input_t, output_t
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return 0, 0
