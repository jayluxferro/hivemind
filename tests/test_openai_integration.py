"""Integration test: OpenAI API format through the full proxy stack.

Spins up mock API in OpenAI mode + HiveMind proxy, sends requests in
OpenAI chat completion format, verifies responses, token counting,
rate limit header parsing, and streaming all work correctly.
"""

import asyncio
import socket

import httpx
import pytest

from evaluation.mock_api import MockAPIConfig, MockAPIServer
from hivemind.proxy.server import ProxyServer
from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController
from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.storage.models import HiveMindConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_for(url: str, timeout: float = 10.0):
    async with httpx.AsyncClient() as c:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await c.get(url, timeout=2.0)
                if r.status_code == 200:
                    return
            except (httpx.ConnectError, httpx.ReadError):
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"{url} not ready")


async def _setup_openai_stack():
    """Spin up mock OpenAI API + HiveMind proxy, return (api_port, proxy_port, tasks)."""
    api_port = _free_port()
    proxy_port = _free_port()

    api = MockAPIServer(MockAPIConfig(
        port=api_port,
        requests_per_minute=100,
        base_latency_ms=10.0,
        latency_jitter_ms=5.0,
        api_format="openai",
    ))
    api_task = asyncio.create_task(api.serve())
    await _wait_for(f"http://127.0.0.1:{api_port}/_health")

    config = HiveMindConfig(
        proxy_host="127.0.0.1",
        proxy_port=proxy_port,
        upstream_url=f"http://127.0.0.1:{api_port}",
        max_concurrency=5,
    )
    proxy = ProxyServer(
        config=config,
        admission=AdmissionController(5),
        rate_limiter=RateLimiter(),
        backpressure=BackpressureController(5),
        budget_manager=BudgetManager(),
    )
    proxy_task = asyncio.create_task(proxy.serve())
    await _wait_for(f"http://127.0.0.1:{proxy_port}/_health")

    return api_port, proxy_port, [api_task, proxy_task]


async def _cleanup(tasks):
    for task in tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, SystemExit):
            pass


@pytest.mark.asyncio
async def test_openai_non_streaming():
    """OpenAI chat completion (non-streaming) through HiveMind proxy."""
    _, proxy_port, tasks = await _setup_openai_stack()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 100,
                },
                headers={
                    "authorization": "Bearer test-key",
                    "content-type": "application/json",
                    "x-hivemind-agent-id": "openai-test",
                },
            )
            assert resp.status_code == 200
            data = resp.json()

            # Verify OpenAI response format
            assert data["object"] == "chat.completion"
            assert len(data["choices"]) == 1
            assert data["choices"][0]["message"]["role"] == "assistant"
            assert data["choices"][0]["finish_reason"] == "stop"
            assert data["usage"]["prompt_tokens"] > 0
            assert data["usage"]["completion_tokens"] > 0

            # HiveMind scheduling headers present
            assert "x-hivemind-tokens-in" in resp.headers
            assert "x-hivemind-tokens-out" in resp.headers
            assert int(resp.headers["x-hivemind-tokens-in"]) > 0
            assert int(resp.headers["x-hivemind-tokens-out"]) > 0
    finally:
        await _cleanup(tasks)


@pytest.mark.asyncio
async def test_openai_streaming():
    """OpenAI chat completion (streaming) through HiveMind proxy."""
    _, proxy_port, tasks = await _setup_openai_stack()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 100,
                    "stream": True,
                },
                headers={
                    "authorization": "Bearer test-key",
                    "content-type": "application/json",
                    "x-hivemind-agent-id": "openai-stream-test",
                },
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")

                chunks = []
                async for chunk in resp.aiter_bytes():
                    text = chunk.decode("utf-8", errors="replace")
                    chunks.append(text)

                all_text = "".join(chunks)
                # Verify OpenAI SSE format
                assert "chat.completion.chunk" in all_text, "Should see OpenAI chunk objects"
                assert '"delta"' in all_text, "Should see delta fields in chunks"
                assert '"finish_reason": "stop"' in all_text, "Should see stop finish_reason"
                assert "[DONE]" in all_text, "Should see [DONE] sentinel"
                assert "completion_tokens" in all_text, "Should see usage with completion_tokens"
    finally:
        await _cleanup(tasks)


@pytest.mark.asyncio
async def test_openai_rate_limit_headers():
    """Verify OpenAI-format rate limit headers are returned and parsed."""
    _, proxy_port, tasks = await _setup_openai_stack()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={
                    "authorization": "Bearer test-key",
                    "content-type": "application/json",
                },
            )
            assert resp.status_code == 200

            # OpenAI-format rate limit headers should be passed through
            assert "x-ratelimit-remaining-requests" in resp.headers
            assert "x-ratelimit-remaining-tokens" in resp.headers
            remaining = int(resp.headers["x-ratelimit-remaining-requests"])
            assert remaining >= 0
    finally:
        await _cleanup(tasks)


@pytest.mark.asyncio
async def test_openai_token_counting_accuracy():
    """Verify token counts extracted from OpenAI responses match what the mock sent."""
    _, proxy_port, tasks = await _setup_openai_stack()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hello world"}],
                    "max_tokens": 100,
                },
                headers={
                    "authorization": "Bearer test-key",
                    "content-type": "application/json",
                    "x-hivemind-agent-id": "token-test",
                },
            )
            assert resp.status_code == 200
            data = resp.json()

            # Token counts from HiveMind headers should match the response usage
            hivemind_in = int(resp.headers["x-hivemind-tokens-in"])
            hivemind_out = int(resp.headers["x-hivemind-tokens-out"])

            assert hivemind_in == data["usage"]["prompt_tokens"]
            assert hivemind_out == data["usage"]["completion_tokens"]
    finally:
        await _cleanup(tasks)
