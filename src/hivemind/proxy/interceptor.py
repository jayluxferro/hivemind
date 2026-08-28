"""Request/response interceptor — the core proxy logic.

Sits between agents and the upstream API. For each request:
1. Check circuit breaker (fast-fail if open)
2. Acquire admission slot
3. Wait if rate-limited
4. Forward request to upstream
5. Parse rate limit headers from response
6. Record latency for backpressure
7. Count tokens for budget management
8. Retry transparently on 429/502/ECONNRESET
9. Release admission slot
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..scheduler.admission import AdmissionController
from ..scheduler.backpressure import BackpressureController
from ..scheduler.budget import BudgetExhausted, BudgetManager
from ..scheduler.providers import ProviderProfile, detect_provider
from ..scheduler.rate_limiter import RateLimiter
from .latency_tracker import LatencyTracker
from .retry import RetryPolicy, is_retryable_error, is_retryable_status
from .streaming import StreamingResult, is_sse_content_type, is_streaming_request, parse_sse_chunk, sse_terminal_error_frame, stream_response
from .token_counter import count_request_tokens, count_response_tokens
from ..scheduler.cache_telemetry import CacheTelemetry

logger = logging.getLogger(__name__)

# Headers never forwarded upstream: hop-by-hop framing plus accept-encoding.
# accept-encoding is capability-bound: this proxy consumes the upstream
# response before re-serving it, so it must only advertise encodings its own
# httpx can decode.  Forwarding a client's `br`/`zstd` when brotli/zstandard
# aren't installed makes the upstream (or its CDN) send bytes that reach the
# client raw with content-encoding stripped — undecodable garbage.
_FORWARD_DROP = frozenset({
    "host", "transfer-encoding", "connection", "content-length", "accept-encoding",
})


def _forward_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop + accept-encoding; httpx re-adds its own capability set."""
    return {k: v for k, v in headers.items() if k.lower() not in _FORWARD_DROP}


class InterceptResult:
    """Result of an intercepted request."""

    __slots__ = ("status_code", "headers", "body", "tokens_in", "tokens_out", "latency_ms", "retries")

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        retries: int = 0,
    ) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.latency_ms = latency_ms
        self.retries = retries


class Interceptor:
    """Core proxy interceptor that coordinates all scheduling primitives."""

    def __init__(
        self,
        upstream_url: str,
        admission: AdmissionController,
        rate_limiter: RateLimiter,
        backpressure: BackpressureController,
        budget_manager: BudgetManager,
        latency_tracker: LatencyTracker,
        retry_policy: RetryPolicy,
        provider: ProviderProfile | None = None,
        *,
        tls_verify: bool = True,
        cache_telemetry: CacheTelemetry | None = None,
    ) -> None:
        self.upstream_url = upstream_url.rstrip("/")
        self.admission = admission
        self.rate_limiter = rate_limiter
        self.backpressure = backpressure
        self.budget_manager = budget_manager
        self.latency_tracker = latency_tracker
        self.retry_policy = retry_policy
        self.provider = provider
        self._tls_verify = tls_verify
        self.cache_telemetry = cache_telemetry
        self._client: httpx.AsyncClient | None = None

    def rebind_upstream(self, upstream_url: str, provider: ProviderProfile | None = None) -> None:
        """Point the interceptor at a new upstream (URL and provider profile)."""
        self.upstream_url = upstream_url.rstrip("/")
        self.provider = provider if provider is not None else detect_provider(self.upstream_url)

    async def set_tls_verify(self, verify: bool) -> None:
        """Toggle TLS certificate verification; recreates the httpx client if running."""
        self._tls_verify = verify
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            await self.start()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=True,
            verify=self._tls_verify,
            # Empty user-agent default so httpx doesn't inject python-httpx/X.Y.Z;
            # the actual user-agent flows through via forward_headers from the agent.
            headers={"user-agent": ""},
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Interceptor not started")
        return self._client

    def is_streaming(self, headers: dict[str, str], body: bytes) -> bool:
        """Check if this request expects a streaming response."""
        return is_streaming_request(headers, body)

    async def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        agent_id: str | None = None,
    ) -> InterceptResult:
        """Process a proxied API request through all scheduling layers."""

        # 0. Circuit breaker — fast-fail if the upstream is overwhelmed
        if self.backpressure.circuit_open:
            return InterceptResult(
                status_code=503,
                headers={"content-type": "application/json", "retry-after": "10"},
                body=b'{"error": "HiveMind: circuit breaker open - upstream overwhelmed, retry later"}',
            )

        # Estimate request tokens for budget check
        est_request_tokens = count_request_tokens(body)
        if self.cache_telemetry:
            self.cache_telemetry.observe_request(body)

        # 1. Acquire admission slot
        acquired = await self.admission.acquire(timeout=120.0)
        if not acquired:
            return InterceptResult(
                status_code=503,
                headers={"content-type": "application/json"},
                body=b'{"error": "HiveMind: admission timeout - all slots busy"}',
            )

        try:
            return await self._forward_with_retry(
                method=method,
                path=path,
                headers=headers,
                body=body,
                agent_id=agent_id,
                est_request_tokens=est_request_tokens,
            )
        finally:
            await self.admission.release()

    async def handle_streaming_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        agent_id: str | None = None,
    ):
        """Handle a streaming request — yields (chunk, streaming_result) tuples.

        Implements two gates from the SSE envelope spec:

        * Gate 1 — never commit to ``text/event-stream`` until the upstream
          proves it has one (2xx + ``text/event-stream`` content-type). Errors
          and non-SSE 2xx bodies are returned as plain responses, with retries
          allowed only before any byte has been committed to the client.
        * Gate 2 — once committed, a mid-stream failure emits exactly one
          terminal SSE error frame and then closes. Zero-frame EOF is a bug.

        The admission slot is held for the duration of the request (including
        any retries) and released exactly once.
        """
        # 0. Circuit breaker — fast-fail if the upstream is overwhelmed
        if self.backpressure.circuit_open:
            error_body = b'{"error": "HiveMind: circuit breaker open - upstream overwhelmed, retry later"}'
            yield error_body, StreamingResult(status_code=503, error="circuit breaker open")
            return

        est_request_tokens = count_request_tokens(body)
        if self.cache_telemetry:
            self.cache_telemetry.observe_request(body)

        acquired = await self.admission.acquire(timeout=120.0)
        if not acquired:
            error_body = b'{"error": "HiveMind: admission timeout - all slots busy"}'
            yield error_body, StreamingResult(status_code=503, error="admission timeout")
            return

        try:
            upstream_url = f"{self.upstream_url}{path}"
            forward_headers = _forward_headers(headers)

            start = time.monotonic()
            result = StreamingResult()
            provider_codes = self.provider.retryable_status_codes if self.provider else None

            # Retry loop: only valid while no byte has been sent to the client.
            for attempt in range(self.retry_policy.max_retries + 1):
                response: httpx.Response | None = None
                chunk_queue: asyncio.Queue | None = None
                try:
                    await self.rate_limiter.wait_if_throttled()

                    response, chunk_queue = await stream_response(
                        self.client, method, upstream_url, forward_headers, body,
                    )
                    result.status_code = response.status_code
                    result.headers = dict(response.headers)

                    await self.rate_limiter.update_from_headers(result.headers)

                    # Gate 1 branch: upstream already returned an error status.
                    if response.status_code >= 400:
                        retry_after = None
                        if "retry-after" in result.headers:
                            try:
                                retry_after = float(result.headers["retry-after"])
                            except ValueError:
                                pass

                        if is_retryable_status(response.status_code, provider_codes) and self.retry_policy.should_retry(
                            attempt,
                            status_code=response.status_code,
                            retryable_codes=provider_codes,
                            retry_after=retry_after,
                        ):
                            await self.backpressure.record_error()
                            await self.retry_policy.wait(attempt, retry_after)
                            result.retries += 1
                            await response.aclose()
                            continue

                        error_chunks = []
                        while True:
                            chunk = await chunk_queue.get()
                            if chunk is None:
                                break
                            if isinstance(chunk, Exception):
                                break
                            error_chunks.append(chunk)
                        await response.aclose()
                        latency_ms = (time.monotonic() - start) * 1000
                        self.latency_tracker.record(latency_ms, response.status_code)
                        result.latency_total_ms = latency_ms
                        error_body = b"".join(error_chunks).decode("utf-8", errors="replace")[:500]
                        result.error = error_body

                        if response.status_code == 401:
                            has_key = "x-api-key" in forward_headers
                            key_prefix = forward_headers.get("x-api-key", "")[:8] if has_key else "<missing>"
                            logger.warning(
                                "401 from upstream (stream): url=%s has_x_api_key=%s "
                                "key_prefix=%s anthropic_version=%s body=%s",
                                upstream_url,
                                has_key,
                                key_prefix,
                                forward_headers.get("anthropic-version", "<missing>"),
                                error_body[:300],
                            )

                        yield b"".join(error_chunks), result
                        return

                    # Gate 1 branch: upstream returned 2xx but it isn't an SSE stream.
                    if not is_sse_content_type(result.headers):
                        body_chunks = []
                        while True:
                            chunk = await chunk_queue.get()
                            if chunk is None:
                                break
                            if isinstance(chunk, Exception):
                                # No byte has been sent yet, so this is still a
                                # pre-commit failure we can retry.
                                raise chunk
                            body_chunks.append(chunk)
                        await response.aclose()
                        latency_ms = (time.monotonic() - start) * 1000
                        self.latency_tracker.record(latency_ms, response.status_code)
                        result.latency_total_ms = latency_ms
                        yield b"".join(body_chunks), result
                        return

                    # Committed SSE path: from here on the status is frozen.
                    first_chunk = True
                    total_tokens_in = 0
                    total_tokens_out = 0

                    while True:
                        chunk = await chunk_queue.get()
                        if chunk is None:
                            break

                        if isinstance(chunk, Exception):
                            # If no byte has been committed yet we can still retry.
                            if first_chunk:
                                raise chunk

                            # Gate 2: mid-stream failure — emit exactly one terminal frame.
                            latency_ms = (time.monotonic() - start) * 1000
                            result.latency_total_ms = latency_ms
                            self.latency_tracker.record(latency_ms, result.status_code)
                            await self.backpressure.record_error()
                            result.error = str(chunk)
                            result.tokens_in = total_tokens_in or est_request_tokens
                            result.tokens_out = total_tokens_out
                            yield sse_terminal_error_frame(path, result.error), None
                            yield None, result
                            return

                        # Track first chunk latency only on the first real byte.
                        if first_chunk:
                            result.latency_first_chunk_ms = (time.monotonic() - start) * 1000
                            first_chunk = False

                        result.chunks_sent += 1

                        tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
                        total_tokens_in += tokens_in
                        total_tokens_out += tokens_out
                        if self.cache_telemetry and b'"usage"' in chunk:
                            self.cache_telemetry.observe_response(chunk)

                        yield chunk, None

                    # Normal completion.
                    result.latency_total_ms = (time.monotonic() - start) * 1000
                    result.tokens_in = total_tokens_in or est_request_tokens
                    result.tokens_out = total_tokens_out

                    self.latency_tracker.record(result.latency_total_ms, result.status_code)
                    await self.backpressure.record_latency(result.latency_total_ms)
                    await self.backpressure.record_success()
                    self.rate_limiter.record_tokens(result.tokens_in + result.tokens_out)

                    if agent_id and (result.tokens_in or result.tokens_out):
                        try:
                            await self.budget_manager.record_usage(
                                agent_id, result.tokens_in, result.tokens_out
                            )
                        except BudgetExhausted:
                            logger.warning("Budget exhausted for agent %s during stream", agent_id)

                    yield None, result
                    return

                except Exception as exc:
                    latency_ms = (time.monotonic() - start) * 1000
                    result.latency_total_ms = latency_ms
                    result.error = str(exc)

                    if response is not None:
                        await response.aclose()

                    # Pre-commit failures may be retried before the client sees anything.
                    if is_retryable_error(exc) and self.retry_policy.should_retry(attempt, error=exc):
                        await self.backpressure.record_error()
                        await self.retry_policy.wait(attempt)
                        result.retries += 1
                        continue

                    # Non-retryable or exhausted: map to a real HTTP status.
                    status = 504 if isinstance(exc, httpx.TimeoutException) else 502
                    result.status_code = status
                    result.headers["content-type"] = "application/json"
                    self.latency_tracker.record(latency_ms, status)
                    await self.backpressure.record_error()
                    error_json = f'{{"error": "HiveMind proxy error: {exc}"}}'.encode()
                    yield error_json, result
                    return

            # Retries exhausted — should have returned inside the loop, but guard anyway.
            result.status_code = 502
            result.headers["content-type"] = "application/json"
            result.error = result.error or "max retries exceeded"
            latency_ms = (time.monotonic() - start) * 1000
            self.latency_tracker.record(latency_ms, 502)
            await self.backpressure.record_error()
            yield f'{{"error": "HiveMind: retries exhausted - {result.error}"}}'.encode(), result

        finally:
            await self.admission.release()

    async def _forward_with_retry(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        agent_id: str | None,
        est_request_tokens: int,
    ) -> InterceptResult:
        """Forward request with transparent retry on failure."""

        last_error: Exception | None = None
        retries = 0

        for attempt in range(self.retry_policy.max_retries + 1):
            # 2. Wait if rate-limited
            await self.rate_limiter.wait_if_throttled()

            # 3. Forward request
            start = time.monotonic()
            try:
                upstream_url = f"{self.upstream_url}{path}"

                # Clean hop-by-hop + capability-bound headers
                forward_headers = _forward_headers(headers)

                response = await self.client.request(
                    method=method,
                    url=upstream_url,
                    headers=forward_headers,
                    content=body,
                )

                latency_ms = (time.monotonic() - start) * 1000

                # Debug: log auth failures with response body and header presence
                if response.status_code == 401:
                    has_key = "x-api-key" in forward_headers
                    key_prefix = forward_headers.get("x-api-key", "")[:8] if has_key else "<missing>"
                    logger.warning(
                        "401 from upstream: url=%s has_x_api_key=%s key_prefix=%s "
                        "anthropic_version=%s body=%s",
                        upstream_url,
                        has_key,
                        key_prefix,
                        forward_headers.get("anthropic-version", "<missing>"),
                        response.content[:300],
                    )

                # 4. Record latency
                self.latency_tracker.record(latency_ms, response.status_code)
                await self.backpressure.record_latency(latency_ms)

                # 5. Parse rate limit headers
                resp_headers = dict(response.headers)
                await self.rate_limiter.update_from_headers(resp_headers)

                # 6. Check if retryable (use provider-specific codes when available)
                retry_after = None
                if "retry-after" in resp_headers:
                    try:
                        retry_after = float(resp_headers["retry-after"])
                    except ValueError:
                        pass
                provider_codes = self.provider.retryable_status_codes if self.provider else None
                if is_retryable_status(response.status_code, provider_codes) and self.retry_policy.should_retry(attempt, response.status_code, retryable_codes=provider_codes, retry_after=retry_after):
                    await self.backpressure.record_error()
                    await self.retry_policy.wait(attempt, retry_after)
                    retries += 1
                    continue

                # 7. Count tokens
                tokens_in, tokens_out = count_response_tokens(response.content)
                if self.cache_telemetry:
                    self.cache_telemetry.observe_response(response.content)
                if not tokens_in:
                    tokens_in = est_request_tokens

                # 8. Record budget usage
                if agent_id:
                    try:
                        await self.budget_manager.record_usage(agent_id, tokens_in, tokens_out)
                    except BudgetExhausted:
                        logger.warning("Budget exhausted for agent %s", agent_id)
                        # Still return the response — budget enforcement is advisory at proxy level

                await self.backpressure.record_success()
                self.rate_limiter.record_tokens(tokens_in + tokens_out)

                return InterceptResult(
                    status_code=response.status_code,
                    headers=resp_headers,
                    body=response.content,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    latency_ms=latency_ms,
                    retries=retries,
                )

            except Exception as exc:
                latency_ms = (time.monotonic() - start) * 1000
                self.latency_tracker.record(latency_ms, status_code=None)
                last_error = exc

                if is_retryable_error(exc) and self.retry_policy.should_retry(attempt, error=exc):
                    await self.backpressure.record_error()
                    await self.retry_policy.wait(attempt)
                    retries += 1
                    continue

                logger.error("Interceptor: non-retryable error: %s", exc)
                return InterceptResult(
                    status_code=502,
                    headers={"content-type": "application/json"},
                    body=f'{{"error": "HiveMind proxy error: {exc}"}}'.encode(),
                    latency_ms=latency_ms,
                    retries=retries,
                )

        # All retries exhausted
        error_msg = str(last_error) if last_error else "max retries exceeded"
        logger.error("Interceptor: all retries exhausted: %s", error_msg)
        return InterceptResult(
            status_code=502,
            headers={"content-type": "application/json"},
            body=f'{{"error": "HiveMind: retries exhausted - {error_msg}"}}'.encode(),
            retries=retries,
        )
