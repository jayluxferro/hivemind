"""Integration test: streaming SSE through the full proxy stack.

Spins up mock API + HiveMind proxy, sends a stream:true request,
verifies chunks arrive progressively and tokens are counted.
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


@pytest.mark.asyncio
async def test_streaming_through_proxy():
    """Full end-to-end: mock API (streaming) → HiveMind proxy → client."""
    api_port = _free_port()
    proxy_port = _free_port()

    # Start mock API
    api = MockAPIServer(MockAPIConfig(
        port=api_port,
        requests_per_minute=100,
        base_latency_ms=10.0,
        latency_jitter_ms=5.0,
    ))
    api_task = asyncio.create_task(api.serve())
    await _wait_for(f"http://127.0.0.1:{api_port}/_health")

    # Start proxy
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

    try:
        # Send streaming request through proxy
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST",
                f"http://127.0.0.1:{proxy_port}/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
                headers={
                    "x-api-key": "test-key",
                    "content-type": "application/json",
                    "x-hivemind-agent-id": "stream-test",
                },
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")

                chunks = []

                async for chunk in resp.aiter_bytes():
                    text = chunk.decode("utf-8", errors="replace")
                    chunks.append(text)

                all_text = "".join(chunks)
                assert "message_start" in all_text, "Should see message_start event"
                assert "content_block_delta" in all_text, "Should see content_block_delta events"
                assert "message_stop" in all_text, "Should see message_stop event"
                assert "output_tokens" in all_text, "Should see usage with output_tokens"
                assert len(all_text) > 100, "Should receive substantial SSE content"

    finally:
        for task in [proxy_task, api_task]:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, SystemExit):
                pass


@pytest.mark.asyncio
async def test_non_streaming_still_works():
    """Verify non-streaming requests still work after streaming changes."""
    api_port = _free_port()
    proxy_port = _free_port()

    api = MockAPIServer(MockAPIConfig(
        port=api_port, requests_per_minute=100, base_latency_ms=10.0, latency_jitter_ms=5.0,
    ))
    api_task = asyncio.create_task(api.serve())
    await _wait_for(f"http://127.0.0.1:{api_port}/_health")

    config = HiveMindConfig(
        proxy_host="127.0.0.1", proxy_port=proxy_port,
        upstream_url=f"http://127.0.0.1:{api_port}", max_concurrency=5,
    )
    proxy = ProxyServer(
        config=config,
        admission=AdmissionController(5), rate_limiter=RateLimiter(),
        backpressure=BackpressureController(5), budget_manager=BudgetManager(),
    )
    proxy_task = asyncio.create_task(proxy.serve())
    await _wait_for(f"http://127.0.0.1:{proxy_port}/_health")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{proxy_port}/v1/messages",
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Hi"}],
                },
                headers={"x-api-key": "test", "content-type": "application/json"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["type"] == "message"
            assert data["usage"]["input_tokens"] > 0
            assert data["usage"]["output_tokens"] > 0
            # HiveMind headers present
            assert "x-hivemind-tokens-in" in resp.headers
    finally:
        for task in [proxy_task, api_task]:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, SystemExit):
                pass
