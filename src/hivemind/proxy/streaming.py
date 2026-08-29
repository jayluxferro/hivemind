"""Streaming SSE pass-through with token counting.

Forwards Server-Sent Events (text/event-stream) chunks as they arrive
while extracting token usage from the final message_delta/done event.
Agents get real-time streaming; HiveMind still counts everything.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class StreamingResult:
    """Accumulated metrics from a streaming response."""

    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_first_chunk_ms: float = 0.0
    latency_total_ms: float = 0.0
    chunks_sent: int = 0
    retries: int = 0
    error: str | None = None


def is_streaming_request(headers: dict[str, str], body: bytes) -> bool:
    """Check if the request expects a streaming response."""
    # Check accept header
    accept = headers.get("accept", "")
    if "text/event-stream" in accept:
        return True
    # Check request body for stream: true
    try:
        data = json.loads(body)
        return data.get("stream", False) is True
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def parse_sse_chunk(raw: bytes) -> tuple[int, int, bool]:
    """Parse an SSE chunk for token usage.

    Returns (input_tokens, output_tokens, is_final).
    Most chunks return (0, 0, False). Only the final event has usage data.
    """
    try:
        text = raw.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            line = line.strip()

            # Check event type
            if line.startswith("event: "):
                event_type = line[7:].strip()
                if event_type in ("message_stop", "done", "error"):
                    # This might be final but we need the data line for usage
                    pass

            if not line.startswith("data: "):
                continue

            payload = line[6:]
            if payload == "[DONE]":
                return 0, 0, True

            data = json.loads(payload)

            # Check for usage in this chunk (Anthropic sends it in message_delta)
            usage = data.get("usage", {})
            if usage:
                input_t = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                output_t = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                if input_t or output_t:
                    return input_t, output_t, False

            # Anthropic message_start has input token count in message.usage
            msg = data.get("message", {})
            msg_usage = msg.get("usage", {})
            if msg_usage:
                input_t = msg_usage.get("input_tokens", 0)
                return input_t, 0, False

            # Check for stop event
            event_type = data.get("type", "")
            if event_type in ("message_stop",):
                return 0, 0, True

            # OpenAI finish_reason
            choices = data.get("choices", [])
            for choice in choices:
                if choice.get("finish_reason") is not None:
                    return 0, 0, True

    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    return 0, 0, False


def _sse_path_flavor(path: str) -> str:
    """Detect SSE event flavor from the request path."""
    base = path.split("?", 1)[0]
    if base == "/v1/messages":
        return "anthropic"
    if base == "/v1/chat/completions":
        return "openai"
    # Fallback: heuristic for any anthropic-style messages endpoint.
    return "anthropic" if "/v1/messages" in base else "openai"


def is_sse_content_type(headers: dict[str, str]) -> bool:
    """Return True if the upstream headers advertise a text/event-stream body."""
    return "text/event-stream" in headers.get("content-type", "")


def sse_terminal_error_frame(path: str, message: str) -> bytes:
    """Build a single terminal SSE frame for a mid-stream failure (Gate 2).

    Anthropic /v1/messages uses ``event: error``. OpenAI /v1/chat/completions
    uses a ``data: {"error": ...}`` frame followed by ``data: [DONE]``.

    The leading ``\\n\\n`` self-frames the terminal event: if the upstream died
    mid-frame, the partial line is terminated and its block dispatched first;
    after a complete frame the extra blank lines are no-ops to SSE parsers.
    """
    flavor = _sse_path_flavor(path)
    if flavor == "anthropic":
        return (f"\n\nevent: error\ndata: {json.dumps({'error': {'type': 'error', 'message': message}})}\n\n").encode()
    # OpenAI flavor
    return (
        f"\n\ndata: {json.dumps({'error': {'message': message, 'type': 'api_error'}})}\n\ndata: [DONE]\n\n"
    ).encode()


async def stream_response(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[httpx.Response, asyncio.Queue]:
    """Initiate a streaming request and return the response + a chunk queue.

    The caller should iterate over the queue to get chunks.
    A None sentinel signals the stream is complete.
    """
    # Use httpx streaming
    req = client.build_request(method, url, headers=headers, content=body)
    response = await client.send(req, stream=True)

    chunk_queue: asyncio.Queue = asyncio.Queue()

    async def _read_stream():
        try:
            async for chunk in response.aiter_bytes():
                await chunk_queue.put(chunk)
        except Exception as exc:
            await chunk_queue.put(exc)
        finally:
            await response.aclose()
            await chunk_queue.put(None)  # Sentinel

    asyncio.create_task(_read_stream())
    return response, chunk_queue
