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
import json
import logging
import time

import httpx

from ..scheduler.admission import AdmissionController
from ..scheduler.backpressure import BackpressureController
from ..scheduler.budget import BudgetExhausted, BudgetManager
from ..scheduler.providers import ProviderProfile, detect_provider
from ..scheduler.rate_limiter import RateLimiter, ThrottleWaitExceeded
from .latency_tracker import LatencyTracker
from .retry import RetryPolicy, is_retryable_error, is_retryable_status
from .streaming import (
    StreamingResult,
    is_sse_content_type,
    is_streaming_request,
    parse_sse_chunk,
    sse_terminal_error_frame,
    stream_response,
)
from .token_counter import count_request_tokens, count_response_tokens
from ..scheduler.cache_telemetry import (
    CacheTelemetry,
    extract_cache_usage,
    extract_cache_usage_from_sse,
)
from ..telemetry.ledger import get_ledger

logger = logging.getLogger(__name__)

# Headers never forwarded upstream: hop-by-hop framing plus accept-encoding.
# accept-encoding is capability-bound: this proxy consumes the upstream
# response before re-serving it, so it must only advertise encodings its own
# httpx can decode.  Forwarding a client's `br`/`zstd` when brotli/zstandard
# aren't installed makes the upstream (or its CDN) send bytes that reach the
# client raw with content-encoding stripped — undecodable garbage.
_FORWARD_DROP = frozenset(
    {
        "host",
        "transfer-encoding",
        "connection",
        "content-length",
        "accept-encoding",
    }
)


def _forward_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop + accept-encoding; httpx re-adds its own capability set."""
    return {k: v for k, v in headers.items() if k.lower() not in _FORWARD_DROP}


def _model_from_request_body(body: bytes) -> str:
    """Model name observed from the request body; ``"unknown"`` when absent."""
    try:
        data = json.loads(body)
        model = data.get("model") if isinstance(data, dict) else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        model = None
    return model if isinstance(model, str) and model else "unknown"


def _discard_task_exception(task: asyncio.Task) -> None:
    """Consume a fire-and-forget task's exception so it never logs as unretrieved.

    The ledger hooks schedule ``ledger.record(row)`` as a background task and
    never await it (fail-open, D4): without a done callback, a raising task
    emits "Task exception was never retrieved" noise at GC time.
    """
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


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
            # read=240.0 is DELIBERATELY below the rest of the chain's 300s
            # ceiling: a stalled upstream must fail HERE first so the typed
            # 504 propagates up before every upstream gateway aborts its own
            # 300s read (which surfaced as a bare ReadTimeout with zero
            # downstream logs, 2026-09-01).
            timeout=httpx.Timeout(connect=10.0, read=240.0, write=30.0, pool=30.0),
            # The pool must not be the admission bottleneck: admission allows
            # up to max_concurrency (240 in the manifold config) in-flight
            # requests, but a 50-connection pool silently queued everything
            # past the 50th at the LAST hop.  Match the admission ceiling.
            limits=httpx.Limits(max_connections=240, max_keepalive_connections=100),
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
        rate_key: str | None = None,
    ) -> InterceptResult:
        """Process a proxied API request through all scheduling layers.

        agent_id is the explicit identity (budgets/logging); rate_key is the
        rate-limiter bucket — defaults to agent_id when not provided.

        Records one telemetry row AFTER the result is decided, on every exit
        path (circuit-open, admission timeout, upstream result) — exactly
        once, fire-and-forget, never raising (SPEC-token-ledger D4).
        """

        # 0. Circuit breaker — fast-fail if the upstream is overwhelmed
        if self.backpressure.circuit_open:
            result = InterceptResult(
                status_code=503,
                headers={"content-type": "application/json", "retry-after": "10"},
                body=b'{"error": "HiveMind: circuit breaker open - upstream overwhelmed, retry later"}',
            )
        else:
            # Estimate request tokens for budget check
            est_request_tokens = count_request_tokens(body)
            if self.cache_telemetry:
                self.cache_telemetry.observe_request(body)

            # 1. Acquire admission slot
            acquired = await self.admission.acquire(timeout=120.0)
            if not acquired:
                result = InterceptResult(
                    status_code=503,
                    headers={"content-type": "application/json"},
                    body=b'{"error": "HiveMind: admission timeout - all slots busy"}',
                )
            else:
                try:
                    result = await self._forward_with_retry(
                        method=method,
                        path=path,
                        headers=headers,
                        body=body,
                        agent_id=agent_id,
                        rate_key=rate_key,
                        est_request_tokens=est_request_tokens,
                    )
                finally:
                    await self.admission.release()

        # Ledger hook: AFTER the response completes; swallowed, never awaited.
        self._record_usage(result, body=body, agent_id=agent_id, rate_key=rate_key)
        return result

    def _record_usage(
        self,
        result: InterceptResult | StreamingResult,
        *,
        body: bytes,
        agent_id: str | None,
        rate_key: str | None,
    ) -> None:
        """Fire-and-forget ledger write for one completed request (D4).

        Schedules ``ledger.record(row)`` as a background task and returns
        immediately — the request path never awaits Postgres.  Any failure
        (including a hostile ledger) is logged at DEBUG and swallowed.
        """
        try:
            row = self._usage_row(result, body=body, agent_id=agent_id, rate_key=rate_key)
            ledger = get_ledger()
            task = asyncio.get_running_loop().create_task(ledger.record(row))
            task.add_done_callback(_discard_task_exception)
        except Exception:
            logger.debug("telemetry record hook failed (fail-open)", exc_info=True)

    def _usage_row(
        self,
        result: InterceptResult | StreamingResult,
        *,
        body: bytes,
        agent_id: str | None,
        rate_key: str | None,
    ) -> dict:
        """Build the ledger row for a completed result (SPEC-token-ledger §4).

        Bucket precedence: rate_key (rate-limiter bucket, already hashed),
        else agent_id, else ``"anonymous"``.  Provider/model are OBSERVED
        values (D2): the profile display name, and the model from the request
        body.  Usage columns are optional — providers vary; missing or zero
        counts record as NULL rather than a guessed number.
        """
        cache_read = getattr(result, "_cache_read_tokens", None)
        cache_write = getattr(result, "_cache_write_tokens", None)
        if cache_read is None and cache_write is None:
            # Buffered non-streaming path: pull cache usage from the body.
            fields = extract_cache_usage(getattr(result, "body", b""))
            cache_read = (fields.get("cache_read_input_tokens", 0) + fields.get("cached_tokens", 0)) or None
            cache_write = fields.get("cache_creation_input_tokens", 0) or None

        latency = getattr(result, "latency_ms", None)
        if latency is None:
            latency = getattr(result, "latency_total_ms", None)

        def _count(value) -> int | None:
            return value if isinstance(value, int) and value > 0 else None

        return {
            "agent_hash": (rate_key or agent_id) or "anonymous",
            "provider": self.provider.name if self.provider else "unknown",
            "model": _model_from_request_body(body),
            "tokens_in": _count(getattr(result, "tokens_in", 0)),
            "tokens_out": _count(getattr(result, "tokens_out", 0)),
            "cache_read": cache_read,
            "cache_write": cache_write,
            "reasoning": None,  # no provider in the chain reports it separately yet
            "latency_ms": latency or None,
            "status": getattr(result, "status_code", None) or 200,
        }

    async def handle_streaming_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        agent_id: str | None = None,
        rate_key: str | None = None,
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

        The whole body sits inside ONE try whose finally records the telemetry
        row (SPEC-token-ledger §4).  Recording must live in an outermost
        finally, not in code after a yield: the server closes the generator
        (GeneratorExit) right after the FIRST yield of an early error, which
        would skip any trailing statements.  A finally runs on normal
        completion, mid-stream failure, early aclose and cancellation alike —
        and a generator is single-pass, so the hook fires exactly once per
        request.
        """
        # Bookkeeping visible to the finally on every exit path.
        result = StreamingResult()
        cache_read_tokens = 0
        cache_write_tokens = 0
        try:
            # 0. Circuit breaker — fast-fail if the upstream is overwhelmed
            if self.backpressure.circuit_open:
                error_body = b'{"error": "HiveMind: circuit breaker open - upstream overwhelmed, retry later"}'
                result = StreamingResult(status_code=503, error="circuit breaker open")
                yield error_body, result
                return

            est_request_tokens = count_request_tokens(body)
            if self.cache_telemetry:
                self.cache_telemetry.observe_request(body)

            acquired = await self.admission.acquire(timeout=120.0)
            if not acquired:
                error_body = b'{"error": "HiveMind: admission timeout - all slots busy"}'
                result = StreamingResult(status_code=503, error="admission timeout")
                yield error_body, result
                return

            try:
                upstream_url = f"{self.upstream_url}{path}"
                forward_headers = _forward_headers(headers)
                # Rate-limit bucket: explicit rate_key, else fall back to agent_id
                # so direct callers still get per-agent windows when identified.
                bucket = rate_key if rate_key is not None else agent_id

                start = time.monotonic()
                provider_codes = self.provider.retryable_status_codes if self.provider else None

                # Retry loop: only valid while no byte has been sent to the client.
                for attempt in range(self.retry_policy.max_retries + 1):
                    response: httpx.Response | None = None
                    chunk_queue: asyncio.Queue | None = None
                    try:
                        try:
                            await self.rate_limiter.wait_if_throttled(agent_id=bucket)
                        except ThrottleWaitExceeded as exc:
                            # Fail fast instead of queueing for minutes: the client
                            # retries later with a real signal instead of hanging
                            # until every layer's read timeout aborts (2026-09-01).
                            result = StreamingResult(
                                status_code=429,
                                headers={"retry-after": str(max(1, int(exc.wait_s)))},
                                error="rate_limit_queue_full",
                            )
                            yield (
                                b'{"error": "HiveMind: rate-limit queue full - retry later"}',
                                result,
                            )
                            return

                        response, chunk_queue = await stream_response(
                            self.client,
                            method,
                            upstream_url,
                            forward_headers,
                            body,
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

                            if is_retryable_status(
                                response.status_code, provider_codes
                            ) and self.retry_policy.should_retry(
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
                        # Cache usage: Anthropic usage blocks are cumulative
                        # snapshots (message_start → message_delta) that can
                        # arrive split across byte chunks — merge per-key
                        # maxima over the stream, then fold onto the result
                        # for the record hook (NULL when the provider never
                        # reported cache fields).
                        cache_read_tokens = 0
                        cache_write_tokens = 0

                        while True:
                            chunk = await chunk_queue.get()
                            if chunk is None:
                                break

                            if isinstance(chunk, Exception):
                                # If no byte has been committed yet we can still retry.
                                if first_chunk:
                                    raise chunk

                                # Gate 2: mid-stream failure — emit exactly one terminal frame.
                                # An abrupt upstream close surfaces as httpx.ReadError wrapping
                                # anyio.EndOfStream — str(exc) is EMPTY, so always prefix the
                                # exception type; without it the warning and the client-visible
                                # terminal frame carry no information.
                                latency_ms = (time.monotonic() - start) * 1000
                                result.latency_total_ms = latency_ms
                                self.latency_tracker.record(latency_ms, result.status_code)
                                await self.backpressure.record_error()
                                detail = f"{type(chunk).__name__}: {chunk}".rstrip(": ")
                                logger.warning(
                                    "HiveMind: mid-stream failure after %.1fs / %d chunks: %s",
                                    latency_ms / 1000,
                                    result.chunks_sent,
                                    detail,
                                )
                                result.error = detail
                                result.tokens_in = total_tokens_in or est_request_tokens
                                result.tokens_out = total_tokens_out
                                result._cache_read_tokens = cache_read_tokens or None
                                result._cache_write_tokens = cache_write_tokens or None
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
                            if b'"usage"' in chunk:
                                if self.cache_telemetry:
                                    self.cache_telemetry.observe_response(chunk)
                                # Ledger copy of the cache numbers: per-key
                                # maxima (the /_stats aggregator's parse above
                                # json.loads()s whole chunks and cannot see
                                # SSE frames; the ledger rows must not miss
                                # them, SPEC-token-ledger §4).
                                for key, value in extract_cache_usage_from_sse(chunk).items():
                                    if key == "cache_creation_input_tokens":
                                        cache_write_tokens = max(cache_write_tokens, value)
                                    else:  # cache_read_input_tokens or cached_tokens
                                        cache_read_tokens = max(cache_read_tokens, value)

                            yield chunk, None

                        # Normal completion.
                        result.latency_total_ms = (time.monotonic() - start) * 1000
                        result.tokens_in = total_tokens_in or est_request_tokens
                        result.tokens_out = total_tokens_out
                        result._cache_read_tokens = cache_read_tokens or None
                        result._cache_write_tokens = cache_write_tokens or None

                        self.latency_tracker.record(result.latency_total_ms, result.status_code)
                        await self.backpressure.record_latency(result.latency_total_ms)
                        await self.backpressure.record_success()
                        self.rate_limiter.record_tokens(result.tokens_in + result.tokens_out, agent_id=bucket)

                        if agent_id and (result.tokens_in or result.tokens_out):
                            try:
                                await self.budget_manager.record_usage(agent_id, result.tokens_in, result.tokens_out)
                            except BudgetExhausted:
                                logger.warning("Budget exhausted for agent %s during stream", agent_id)

                        yield None, result
                        return

                    except Exception as exc:
                        latency_ms = (time.monotonic() - start) * 1000
                        result.latency_total_ms = latency_ms
                        detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
                        result.error = detail

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
                        error_json = f'{{"error": "HiveMind proxy error: {detail}"}}'.encode()
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
        finally:
            # Ledger hook: AFTER the response completes, on every exit path.
            # Fire-and-forget; must never raise (D4).
            try:
                self._record_usage(result, body=body, agent_id=agent_id, rate_key=rate_key)
            except Exception:  # pragma: no cover — _record_usage already swallows
                logger.debug("telemetry record hook failed (fail-open)", exc_info=True)

    async def _forward_with_retry(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        agent_id: str | None,
        est_request_tokens: int,
        rate_key: str | None = None,
    ) -> InterceptResult:
        """Forward request with transparent retry on failure."""

        last_error: Exception | None = None
        retries = 0
        # Rate-limit bucket: explicit rate_key, else agent_id (see streaming path).
        bucket = rate_key if rate_key is not None else agent_id

        for attempt in range(self.retry_policy.max_retries + 1):
            # 2. Wait if rate-limited
            try:
                await self.rate_limiter.wait_if_throttled(agent_id=bucket)
            except ThrottleWaitExceeded as exc:
                # Fail fast instead of queueing for minutes (see streaming path).
                return InterceptResult(
                    status_code=429,
                    headers={"retry-after": str(max(1, int(exc.wait_s)))},
                    body=b'{"error": "HiveMind: rate-limit queue full - retry later"}',
                )

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
                        "401 from upstream: url=%s has_x_api_key=%s key_prefix=%s anthropic_version=%s body=%s",
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
                if is_retryable_status(response.status_code, provider_codes) and self.retry_policy.should_retry(
                    attempt, response.status_code, retryable_codes=provider_codes, retry_after=retry_after
                ):
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
                self.rate_limiter.record_tokens(tokens_in + tokens_out, agent_id=bucket)

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
