"""Local HTTP reverse proxy — the core of HiveMind.

Agents make API calls to http://localhost:8765/v1/messages and this proxy
transparently forwards them to the upstream API while applying all
scheduling primitives (admission, rate limits, backpressure, token budgets).
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
from ..storage.db import Database
from ..storage.models import HiveMindConfig
from .interceptor import Interceptor
from .latency_tracker import LatencyTracker
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
        self.interceptor = Interceptor(
            upstream_url=config.upstream_url,
            admission=admission,
            rate_limiter=rate_limiter,
            backpressure=backpressure,
            budget_manager=budget_manager,
            latency_tracker=self.latency_tracker,
            retry_policy=self.retry_policy,
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

        app = Starlette(
            routes=[
                Route("/_health", health_handler, methods=["GET"]),
                Route("/_stats", stats_handler, methods=["GET"]),
                # Catch-all proxy route
                Route("/{path:path}", proxy_handler, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]),
            ],
            on_startup=[self._on_startup],
            on_shutdown=[self._on_shutdown],
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
        streaming_result = None

        async def chunk_generator():
            nonlocal streaming_result
            async for chunk, result in self.interceptor.handle_streaming_request(
                method=method, path=path, headers=headers, body=body, agent_id=agent_id,
            ):
                if chunk is not None:
                    yield chunk
                if result is not None:
                    streaming_result = result
                    # If error status, yield the error body
                    if result.error and result.status_code >= 400:
                        yield result.error.encode() if isinstance(result.error, str) else result.error

        async def generate_and_log():
            async for chunk in chunk_generator():
                yield chunk

            # Log after stream completes
            if self.db and streaming_result:
                try:
                    await self.db.log_request(
                        agent_id=agent_id,
                        method=method,
                        path=path,
                        status_code=streaming_result.status_code,
                        tokens_in=streaming_result.tokens_in,
                        tokens_out=streaming_result.tokens_out,
                        latency_ms=streaming_result.latency_total_ms,
                        retried=streaming_result.retries > 0,
                        error=streaming_result.error,
                        recorded_at=time.time(),
                    )
                except Exception as exc:
                    logger.warning("Failed to log streaming request: %s", exc)

        return StreamingResponse(
            generate_and_log(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache",
                "connection": "keep-alive",
            },
        )

    def get_stats(self) -> dict:
        return {
            "proxy": {
                "upstream_url": self.config.upstream_url,
                "host": self.config.proxy_host,
                "port": self.config.proxy_port,
            },
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


def main() -> None:
    """CLI entry point for standalone proxy."""
    import argparse

    parser = argparse.ArgumentParser(description="HiveMind API Proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--max-concurrency", type=int, default=5)
    parser.add_argument("--db", default="hivemind.db")
    args = parser.parse_args()

    config = HiveMindConfig(
        proxy_host=args.host,
        proxy_port=args.port,
        upstream_url=args.upstream,
        max_concurrency=args.max_concurrency,
        db_path=args.db,
    )

    config.apply_provider_defaults()
    logger.info("Detected provider: %s", config.provider)

    admission = AdmissionController(config.max_concurrency)
    rate_limiter = RateLimiter()
    backpressure = BackpressureController(config.max_concurrency)
    budget_manager = BudgetManager()

    proxy = ProxyServer(
        config=config,
        admission=admission,
        rate_limiter=rate_limiter,
        backpressure=backpressure,
        budget_manager=budget_manager,
    )

    asyncio.run(proxy.serve())


if __name__ == "__main__":
    main()
