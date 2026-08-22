"""Local HTTP reverse proxy — the core of HiveMind.

Agents make API calls to http://localhost:8765 and this proxy transparently
forwards them to the upstream API (Anthropic, OpenAI, Ollama, Azure, etc.)
while applying all scheduling primitives (admission, rate limits, backpressure,
token budgets). The provider is auto-detected from the upstream URL.
"""

from __future__ import annotations

import asyncio
import logging
import time

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from ..scheduler.admission import AdmissionController
from ..scheduler.backpressure import BackpressureController
from ..scheduler.budget import BudgetManager
from ..scheduler.rate_limiter import RateLimiter
from ..scheduler.providers import detect_provider
from ..storage.db import Database
from ..storage.models import HiveMindConfig
from .interceptor import Interceptor
from .latency_tracker import LatencyTracker
from ..scheduler.cache_telemetry import CacheTelemetry
from .retry import RetryPolicy

logger = logging.getLogger(__name__)


class ProxyServer:
    """ASGI reverse proxy server with all HiveMind scheduling."""

    def __init__(
        self,
        config: HiveMindConfig,
        admission: AdmissionController,
        rate_limiter: RateLimiter,
        backpressure: BackpressureController,
        budget_manager: BudgetManager,
        db: Database | None = None,
    ) -> None:
        self.config = config
        self.admission = admission
        self.rate_limiter = rate_limiter
        self.backpressure = backpressure
        self.budget_manager = budget_manager
        # Wire backpressure directly to admission controller
        self.backpressure.set_admission(self.admission)
        self.db = db
        self.latency_tracker = LatencyTracker()
        self.retry_policy = RetryPolicy(
            max_retries=config.max_retries,
            base_delay=config.retry_base_delay,
            max_delay=config.retry_max_delay,
        )
        provider = detect_provider(config.upstream_url)
        self.cache_telemetry = CacheTelemetry()
        self.interceptor = Interceptor(
            upstream_url=config.upstream_url,
            admission=admission,
            rate_limiter=rate_limiter,
            backpressure=backpressure,
            budget_manager=budget_manager,
            latency_tracker=self.latency_tracker,
            retry_policy=self.retry_policy,
            provider=provider,
            tls_verify=config.http_tls_verify,
            cache_telemetry=self.cache_telemetry,
        )
        self._app: Starlette | None = None
        self._server_task: asyncio.Task | None = None

    def _build_app(self) -> Starlette:
        async def proxy_handler(request: Request) -> Response:
            return await self._handle_request(request)

        async def health_handler(request: Request) -> Response:
            return Response(
                content='{"status": "ok"}',
                media_type="application/json",
            )

        async def stats_handler(request: Request) -> Response:
            import json

            stats = self.get_stats()
            return Response(
                content=json.dumps(stats, indent=2),
                media_type="application/json",
            )

        async def cache_probe_handler(request: Request) -> Response:
            """Active prompt-cache probe: send one cache_control-bearing body
            twice; a provider that honors caching reports creation on the first
            request and a cache read on the second."""
            import json as _json

            import httpx as _httpx

            try:
                params = await request.json()
            except Exception:
                params = {}
            model = str(params.get("model") or "claude-sonnet-4-20250514")
            # Anthropic-style caching needs a >=1024-token cacheable prefix;
            # this filler crosses the threshold while staying cheap.
            filler = "prompt cache capability probe payload. " * 220
            probe_body = {
                "model": model,
                "max_tokens": 8,
                "system": [
                    {
                        "type": "text",
                        "text": filler,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": "Reply with: ok"}],
            }
            headers = {
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            for key in ("x-api-key", "authorization"):
                value = request.headers.get(key)
                if value:
                    headers[key] = value
            url = f"{self.config.upstream_url.rstrip('/')}/v1/messages"
            usages = []
            try:
                async with _httpx.AsyncClient(
                    verify=self.config.http_tls_verify, timeout=60.0
                ) as client:
                    for _ in range(2):
                        resp = await client.post(url, json=probe_body, headers=headers)
                        try:
                            usages.append(resp.json().get("usage", {}))
                        except Exception:
                            usages.append(
                                {"_status": resp.status_code, "_raw": resp.text[:200]}
                            )
            except Exception as exc:
                return Response(
                    content=_json.dumps({"error": str(exc)}),
                    status_code=502,
                    media_type="application/json",
                )
            verdict = self.cache_telemetry.record_probe(usages[-1] if usages else {})
            return Response(
                content=_json.dumps(
                    {
                        "model": model,
                        "usages": usages,
                        "cache_supported": verdict,
                        "note": "verdict from the second request's usage fields",
                    },
                    indent=2,
                ),
                media_type="application/json",
            )

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(app):
            await self._on_startup()
            yield
            await self._on_shutdown()

        app = Starlette(
            routes=[
                Route("/_health", health_handler, methods=["GET"]),
                Route("/_stats", stats_handler, methods=["GET"]),
                Route("/_probe/cache", cache_probe_handler, methods=["POST"]),
                # Catch-all proxy route
                Route("/{path:path}", proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
            ],
            lifespan=lifespan,
        )
        return app

    async def _on_startup(self) -> None:
        await self.interceptor.start()
        logger.info(
            "HiveMind proxy started on %s:%d → %s (max_concurrency=%d)",
            self.config.proxy_host,
            self.config.proxy_port,
            self.config.upstream_url,
            self.config.max_concurrency,
        )

    async def _on_shutdown(self) -> None:
        await self.interceptor.stop()
        logger.info("HiveMind proxy stopped")

    async def _handle_request(self, request: Request) -> Response:
        """Handle a proxied request through the interceptor."""
        # Extract agent ID from custom header or query param
        agent_id = request.headers.get("x-hivemind-agent-id")
        if not agent_id:
            agent_id = request.query_params.get("agent_id")

        # Read request body
        body = await request.body()

        # Build path
        path = f"/{request.path_params.get('path', '')}"
        if request.url.query:
            query = str(request.url.query)
            path = f"{path}?{query}"

        req_headers = dict(request.headers)

        # Check if this is a streaming request
        if self.interceptor.is_streaming(req_headers, body):
            return await self._handle_streaming_request(
                request.method, path, req_headers, body, agent_id
            )

        # Non-streaming: buffer full response
        result = await self.interceptor.handle_request(
            method=request.method,
            path=path,
            headers=req_headers,
            body=body,
            agent_id=agent_id,
        )

        # Log to database
        if self.db:
            try:
                await self.db.log_request(
                    agent_id=agent_id,
                    method=request.method,
                    path=path,
                    status_code=result.status_code,
                    tokens_in=result.tokens_in,
                    tokens_out=result.tokens_out,
                    latency_ms=result.latency_ms,
                    retried=result.retries > 0,
                    error=None if result.status_code < 400 else result.body.decode("utf-8", errors="replace")[:500],
                    recorded_at=time.time(),
                )
            except Exception as exc:
                logger.warning("Failed to log request: %s", exc)

        # Build response — pass through upstream headers
        response_headers = {}
        for k, v in result.headers.items():
            if k.lower() not in ("transfer-encoding", "connection", "content-encoding", "content-length"):
                response_headers[k] = v

        response_headers["x-hivemind-tokens-in"] = str(result.tokens_in)
        response_headers["x-hivemind-tokens-out"] = str(result.tokens_out)
        response_headers["x-hivemind-latency-ms"] = str(round(result.latency_ms, 1))
        response_headers["x-hivemind-retries"] = str(result.retries)

        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=response_headers,
        )

    async def _handle_streaming_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes, agent_id: str | None
    ) -> Response:
        """Handle a streaming (SSE) request — forward chunks as they arrive."""
        stream_iter = self.interceptor.handle_streaming_request(
            method=method, path=path, headers=headers, body=body, agent_id=agent_id,
        )

        # Pull the first yield to detect early errors (401, 429, 503, etc.)
        # before committing to a StreamingResponse.
        first_chunk = None
        first_result = None
        async for chunk, result in stream_iter:
            first_chunk = chunk
            first_result = result
            break

        # If the first yield already carries a completed result with an error
        # status, return a plain Response so the upstream status code is preserved.
        if first_result is not None and first_result.status_code >= 400:
            await self._log_result(agent_id, method, path, first_result)
            resp_headers = self._build_response_headers(first_result)
            body_bytes = (
                first_chunk
                if isinstance(first_chunk, bytes)
                else (first_chunk.encode() if first_chunk else b"")
            )
            return Response(
                content=body_bytes,
                status_code=first_result.status_code,
                headers=resp_headers,
            )

        # Normal streaming path — forward remaining chunks.
        streaming_result = first_result

        async def generate_and_log():
            nonlocal streaming_result
            # Yield the first chunk we already pulled.
            if first_chunk is not None:
                yield first_chunk

            async for chunk, result in stream_iter:
                if chunk is not None:
                    yield chunk
                if result is not None:
                    streaming_result = result

            if streaming_result:
                await self._log_result(agent_id, method, path, streaming_result)

        return StreamingResponse(
            generate_and_log(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "connection": "keep-alive",
            },
        )

    def _build_response_headers(self, result) -> dict[str, str]:
        """Build response headers from an intercept result, forwarding upstream headers."""
        response_headers: dict[str, str] = {}
        if hasattr(result, "headers") and result.headers:
            for k, v in result.headers.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-encoding", "content-length"):
                    response_headers[k] = v
        response_headers["x-hivemind-tokens-in"] = str(getattr(result, "tokens_in", 0))
        response_headers["x-hivemind-tokens-out"] = str(getattr(result, "tokens_out", 0))
        latency = getattr(result, "latency_ms", None) or getattr(result, "latency_total_ms", 0)
        response_headers["x-hivemind-latency-ms"] = str(round(latency, 1))
        response_headers["x-hivemind-retries"] = str(getattr(result, "retries", 0))
        return response_headers

    async def _log_result(self, agent_id, method, path, result) -> None:
        """Log a completed request to the database."""
        if not self.db or not result:
            return
        try:
            latency = getattr(result, "latency_ms", None) or getattr(result, "latency_total_ms", 0)
            await self.db.log_request(
                agent_id=agent_id,
                method=method,
                path=path,
                status_code=result.status_code,
                tokens_in=getattr(result, "tokens_in", 0),
                tokens_out=getattr(result, "tokens_out", 0),
                latency_ms=latency,
                retried=getattr(result, "retries", 0) > 0,
                error=getattr(result, "error", None),
                recorded_at=time.time(),
            )
        except Exception as exc:
            logger.warning("Failed to log request: %s", exc)

    def get_stats(self) -> dict:
        return {
            "proxy": {
                "upstream_url": self.config.upstream_url,
                "host": self.config.proxy_host,
                "port": self.config.proxy_port,
            },
            "caching": self.cache_telemetry.snapshot(),
            "admission": self.admission.stats,
            "rate_limiter": self.rate_limiter.stats,
            "backpressure": self.backpressure.stats,
            "budget": self.budget_manager.stats,
            "latency": self.latency_tracker.stats,
            "retry": self.retry_policy.stats,
        }

    @property
    def app(self) -> Starlette:
        if self._app is None:
            self._app = self._build_app()
        return self._app

    async def serve(self) -> None:
        """Run the proxy server (blocking)."""
        config = uvicorn.Config(
            app=self.app,
            host=self.config.proxy_host,
            port=self.config.proxy_port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def start_background(self) -> None:
        """Start the proxy in a background task."""
        self._server_task = asyncio.create_task(self.serve())

    async def stop_background(self) -> None:
        """Stop the background proxy server."""
        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass


def _build_proxy(config: HiveMindConfig) -> ProxyServer:
    """Build a ProxyServer from config, wiring all scheduler components."""
    from ..scheduler.budget import BudgetManager as BM

    config.apply_provider_defaults()
    logger.info(
        "Provider: %s | upstream: %s | concurrency: %d",
        config.provider, config.upstream_url, config.max_concurrency,
    )

    admission = AdmissionController(config.max_concurrency)
    rate_limiter = RateLimiter()
    if config.provider:
        from ..scheduler.providers import get_profile
        rate_limiter.configure_from_profile(get_profile(config.provider))
    rate_limiter.apply_overrides(rpm=config.rpm_limit, tpm=config.tpm_limit)
    backpressure = BackpressureController(
        max_concurrency=config.max_concurrency,
        additive_increase=config.aimd_additive_increase,
        multiplicative_decrease=config.aimd_multiplicative_decrease,
        latency_target_ms=config.latency_target_ms,
        min_concurrency=config.min_concurrency,
    )
    budget_manager = BM(
        total_budget=config.total_token_budget,
        default_agent_budget=config.default_agent_budget,
    )

    return ProxyServer(
        config=config,
        admission=admission,
        rate_limiter=rate_limiter,
        backpressure=backpressure,
        budget_manager=budget_manager,
    )


def run_proxy(config: HiveMindConfig) -> None:
    """Run the proxy with a fully-built config (called from __main__)."""
    proxy = _build_proxy(config)
    try:
        asyncio.run(proxy.serve())
    except KeyboardInterrupt:
        pass


def main() -> None:
    """CLI entry point for standalone proxy (hivemind-proxy script)."""
    import argparse
    import logging

    from ..cli_args import hivemind_config_from_proxy_cli_args, register_proxy_cli_arguments

    parser = argparse.ArgumentParser(
        prog="hivemind-proxy",
        description="HiveMind API proxy (same flags as `hivemind proxy`)",
    )
    register_proxy_cli_arguments(parser, include_log_level=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    config = hivemind_config_from_proxy_cli_args(args)
    run_proxy(config)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
