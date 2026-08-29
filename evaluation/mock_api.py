"""Mock LLM API server for evaluation.

Simulates realistic API behavior including:
- Configurable rate limits (requests per window, tokens per window)
- Rate limit headers (anthropic-ratelimit-* or x-ratelimit-*)
- 429 responses when limits are exceeded
- Random 502/ECONNRESET errors at configurable rates
- Configurable latency (fixed + jitter)
- Token usage in responses
- Streaming support (SSE)
- Both Anthropic and OpenAI response formats (api_format config)

This lets us run the full evaluation without burning real API credits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)


@dataclass
class MockAPIConfig:
    """Configuration for the mock API's behavior."""

    host: str = "127.0.0.1"
    port: int = 9999

    # Rate limits
    requests_per_minute: int = 50
    tokens_per_minute: int = 100_000

    # Error injection
    error_rate: float = 0.0  # Fraction of requests that return 502
    connection_reset_rate: float = 0.0  # Fraction that get ECONNRESET (close connection)

    # Latency
    base_latency_ms: float = 100.0
    latency_jitter_ms: float = 50.0
    latency_spike_rate: float = 0.0  # Fraction of requests with 5x latency
    latency_spike_multiplier: float = 5.0

    # Response generation
    output_tokens_mean: int = 150
    output_tokens_std: int = 50

    # Concurrency tracking
    max_concurrent: int = 0  # 0 = unlimited; >0 = reject with 529 if exceeded

    # API format: "anthropic" or "openai"
    api_format: str = "anthropic"


@dataclass
class _RateLimitState:
    """Internal rate limit tracking."""

    request_count: int = 0
    token_count: int = 0
    window_start: float = field(default_factory=time.time)
    window_seconds: float = 60.0

    def reset_if_needed(self) -> None:
        now = time.time()
        if now - self.window_start >= self.window_seconds:
            self.request_count = 0
            self.token_count = 0
            self.window_start = now

    @property
    def window_remaining_seconds(self) -> float:
        return max(0.0, self.window_seconds - (time.time() - self.window_start))


class MockAPIServer:
    """A fake LLM API that behaves realistically for benchmarking.

    Supports both Anthropic (/v1/messages) and OpenAI (/v1/chat/completions)
    response formats via the api_format config parameter.
    """

    def __init__(self, config: MockAPIConfig | None = None) -> None:
        self.config = config or MockAPIConfig()
        self._rate_state = _RateLimitState()
        self._active_connections = 0
        self._total_requests = 0
        self._total_errors = 0
        self._total_rate_limited = 0
        self._lock = asyncio.Lock()
        self._app: Starlette | None = None

    def _build_app(self) -> Starlette:
        app = Starlette(
            routes=[
                Route("/v1/messages", self._handle_messages, methods=["POST"]),
                Route("/v1/chat/completions", self._handle_messages, methods=["POST"]),
                Route("/_stats", self._handle_stats, methods=["GET"]),
                Route("/_config", self._handle_config, methods=["POST"]),
                Route("/_reset", self._handle_reset, methods=["POST"]),
                Route("/_health", self._handle_health, methods=["GET"]),
            ],
        )
        return app

    @property
    def app(self) -> Starlette:
        if self._app is None:
            self._app = self._build_app()
        return self._app

    async def _handle_messages(self, request: Request) -> Response:
        """Simulate POST /v1/messages."""
        self._total_requests += 1

        # Track concurrency
        async with self._lock:
            self._active_connections += 1
            active = self._active_connections

        try:
            # Check concurrency limit
            if self.config.max_concurrent > 0 and active > self.config.max_concurrent:
                self._total_errors += 1
                return JSONResponse(
                    {"type": "error", "error": {"type": "overloaded_error", "message": "Too many concurrent requests"}},
                    status_code=529,
                )

            # Random connection reset (simulate ECONNRESET)
            if random.random() < self.config.connection_reset_rate:
                self._total_errors += 1
                # Abruptly close — return a 502 since we can't truly reset TCP in ASGI
                return Response(content=b"", status_code=502)

            # Random 502 error
            if random.random() < self.config.error_rate:
                self._total_errors += 1
                return JSONResponse(
                    {"type": "error", "error": {"type": "api_error", "message": "Internal server error"}},
                    status_code=502,
                )

            # Rate limit check
            self._rate_state.reset_if_needed()

            body = await request.body()
            try:
                req_data = json.loads(body)
            except json.JSONDecodeError:
                return JSONResponse(
                    {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid JSON"}},
                    status_code=400,
                )

            # Estimate input tokens
            input_tokens = self._estimate_input_tokens(req_data)

            self._rate_state.request_count += 1
            self._rate_state.token_count += input_tokens

            if self._rate_state.request_count > self.config.requests_per_minute:
                self._total_rate_limited += 1
                reset_seconds = self._rate_state.window_remaining_seconds
                rl_429 = self._rate_limit_429_headers(reset_seconds, "requests")
                error_body = (
                    {
                        "error": {
                            "message": "Rate limit exceeded",
                            "type": "rate_limit_error",
                            "code": "rate_limit_exceeded",
                        }
                    }
                    if self.config.api_format == "openai"
                    else {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limit exceeded"}}
                )
                return JSONResponse(error_body, status_code=429, headers=rl_429)

            if self._rate_state.token_count > self.config.tokens_per_minute:
                self._total_rate_limited += 1
                reset_seconds = self._rate_state.window_remaining_seconds
                rl_429 = self._rate_limit_429_headers(reset_seconds, "tokens")
                error_body = (
                    {
                        "error": {
                            "message": "Token rate limit exceeded",
                            "type": "rate_limit_error",
                            "code": "rate_limit_exceeded",
                        }
                    }
                    if self.config.api_format == "openai"
                    else {
                        "type": "error",
                        "error": {"type": "rate_limit_error", "message": "Token rate limit exceeded"},
                    }
                )
                return JSONResponse(error_body, status_code=429, headers=rl_429)

            # Simulate latency
            latency = self.config.base_latency_ms + random.gauss(0, self.config.latency_jitter_ms)
            if random.random() < self.config.latency_spike_rate:
                latency *= self.config.latency_spike_multiplier
            latency = max(10.0, latency)
            await asyncio.sleep(latency / 1000.0)

            # Generate response
            output_tokens = max(10, int(random.gauss(self.config.output_tokens_mean, self.config.output_tokens_std)))
            self._rate_state.token_count += output_tokens

            # Build rate limit headers (format-specific)
            remaining_requests = max(0, self.config.requests_per_minute - self._rate_state.request_count)
            remaining_tokens = max(0, self.config.tokens_per_minute - self._rate_state.token_count)
            reset_time = self._rate_state.window_start + self._rate_state.window_seconds

            msg_id = f"msg_{random.randint(10000, 99999)}"
            is_openai = self.config.api_format == "openai"
            model = req_data.get("model", "gpt-4o" if is_openai else "claude-sonnet-4-20250514")

            if is_openai:
                rl_headers = {
                    "x-ratelimit-limit-requests": str(self.config.requests_per_minute),
                    "x-ratelimit-remaining-requests": str(remaining_requests),
                    "x-ratelimit-reset-requests": str(round(reset_time, 1)),
                    "x-ratelimit-limit-tokens": str(self.config.tokens_per_minute),
                    "x-ratelimit-remaining-tokens": str(remaining_tokens),
                    "x-ratelimit-reset-tokens": str(round(reset_time, 1)),
                }
            else:
                rl_headers = {
                    "anthropic-ratelimit-requests-limit": str(self.config.requests_per_minute),
                    "anthropic-ratelimit-requests-remaining": str(remaining_requests),
                    "anthropic-ratelimit-requests-reset": str(round(reset_time, 1)),
                    "anthropic-ratelimit-tokens-limit": str(self.config.tokens_per_minute),
                    "anthropic-ratelimit-tokens-remaining": str(remaining_tokens),
                    "anthropic-ratelimit-tokens-reset": str(round(reset_time, 1)),
                }

            # Streaming response
            if req_data.get("stream", False):
                if is_openai:
                    return StreamingResponse(
                        self._openai_sse_generator(msg_id, model, input_tokens, output_tokens),
                        media_type="text/event-stream",
                        headers={**rl_headers, "cache-control": "no-cache"},
                    )

                return StreamingResponse(
                    self._anthropic_sse_generator(msg_id, model, input_tokens, output_tokens),
                    media_type="text/event-stream",
                    headers={**rl_headers, "cache-control": "no-cache"},
                )

            # Non-streaming response
            if is_openai:
                response_data = {
                    "id": f"chatcmpl-{msg_id}",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "x " * output_tokens},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": input_tokens,
                        "completion_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                }
            else:
                response_data = {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "x " * output_tokens}],
                    "model": model,
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    },
                }

            return JSONResponse(response_data, headers=rl_headers)

        finally:
            async with self._lock:
                self._active_connections -= 1

    def _rate_limit_429_headers(self, reset_seconds: float, kind: str) -> dict[str, str]:
        """Build 429 rate limit headers in the correct format."""
        base = {"retry-after": str(round(reset_seconds, 1))}
        reset_time = str(round(time.time() + reset_seconds, 1))
        if self.config.api_format == "openai":
            base[f"x-ratelimit-limit-{kind}"] = str(
                self.config.requests_per_minute if kind == "requests" else self.config.tokens_per_minute
            )
            base[f"x-ratelimit-remaining-{kind}"] = "0"
            base[f"x-ratelimit-reset-{kind}"] = reset_time
        else:
            base[f"anthropic-ratelimit-{kind}-limit"] = str(
                self.config.requests_per_minute if kind == "requests" else self.config.tokens_per_minute
            )
            base[f"anthropic-ratelimit-{kind}-remaining"] = "0"
            base[f"anthropic-ratelimit-{kind}-reset"] = reset_time
        return base

    async def _anthropic_sse_generator(self, msg_id: str, model: str, input_tokens: int, output_tokens: int):
        """Generate Anthropic-format SSE stream."""
        # message_start with input usage
        yield f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model, 'usage': {'input_tokens': input_tokens}}})}\n\n"
        # content_block_start
        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        # content deltas
        text = "x " * output_tokens
        chunk_size = max(1, len(text) // 5)
        for i in range(0, len(text), chunk_size):
            chunk_text = text[i : i + chunk_size]
            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': chunk_text}})}\n\n"
            await asyncio.sleep(0.01)
        # content_block_stop
        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        # message_delta with output usage
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': output_tokens}})}\n\n"
        # message_stop
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

    async def _openai_sse_generator(self, msg_id: str, model: str, input_tokens: int, output_tokens: int):
        """Generate OpenAI-format SSE stream."""
        chat_id = f"chatcmpl-{msg_id}"
        # Content deltas
        text = "x " * output_tokens
        chunk_size = max(1, len(text) // 5)
        for i in range(0, len(text), chunk_size):
            chunk_text = text[i : i + chunk_size]
            chunk_data = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk_text}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
            await asyncio.sleep(0.01)
        # Final chunk with finish_reason and usage
        final_data = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }
        yield f"data: {json.dumps(final_data)}\n\n"
        yield "data: [DONE]\n\n"

    def _estimate_input_tokens(self, data: dict) -> int:
        """Rough input token estimate from request data."""
        total = 0
        if "system" in data:
            total += len(str(data["system"])) // 4
        for msg in data.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and "text" in block:
                        total += len(block["text"]) // 4
        return max(10, total)

    async def _handle_stats(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "total_requests": self._total_requests,
                "total_errors": self._total_errors,
                "total_rate_limited": self._total_rate_limited,
                "active_connections": self._active_connections,
                "rate_state": {
                    "request_count": self._rate_state.request_count,
                    "token_count": self._rate_state.token_count,
                    "window_remaining_seconds": round(self._rate_state.window_remaining_seconds, 1),
                },
            }
        )

    async def _handle_config(self, request: Request) -> JSONResponse:
        """Update config on the fly."""
        data = await request.json()
        for key, value in data.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        return JSONResponse({"status": "updated", "config": self.config.__dict__})

    async def _handle_reset(self, request: Request) -> JSONResponse:
        """Reset all counters."""
        self._total_requests = 0
        self._total_errors = 0
        self._total_rate_limited = 0
        self._rate_state = _RateLimitState()
        return JSONResponse({"status": "reset"})

    async def _handle_health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def serve(self) -> None:
        config = uvicorn.Config(
            app=self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()


def main() -> None:
    """Run standalone mock API server."""
    import argparse

    parser = argparse.ArgumentParser(description="Mock Anthropic API for HiveMind evaluation")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--requests-per-minute", type=int, default=50)
    parser.add_argument("--error-rate", type=float, default=0.0)
    parser.add_argument("--base-latency-ms", type=float, default=100.0)
    args = parser.parse_args()

    config = MockAPIConfig(
        port=args.port,
        requests_per_minute=args.requests_per_minute,
        error_rate=args.error_rate,
        base_latency_ms=args.base_latency_ms,
    )

    logging.basicConfig(level=logging.INFO)
    logger.info(
        "Starting mock API on :%d (rate=%d req/min, errors=%.0f%%)",
        config.port,
        config.requests_per_minute,
        config.error_rate * 100,
    )
    asyncio.run(MockAPIServer(config).serve())


if __name__ == "__main__":
    main()
