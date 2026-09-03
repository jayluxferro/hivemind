"""Fire-and-forget token-usage ledger (Postgres) + dashboard reads.

One writer (SPEC D1): the interceptor records a row per cloud-bound request
AFTER the response completes — agent bucket (already-hashed), observed
provider/model, token + cache counters, latency, final status.  No prompt
content ever touches this module (D5).

Fail-open contract (D4): Postgres being down — or the DSN unset — must never
affect the proxy.  Every public entry point swallows its own errors and logs
at DEBUG on ``hivemind.telemetry.ledger``.  Writes are scheduled onto a
bounded asyncio queue drained by one background task, so the request path
does nothing slower than a queue put (fire-and-forget).  On a failed write
the connection is dropped; the next write reconnects lazily and the failed
row is dropped.

Schema is self-managed (D6): connect() runs CREATE SCHEMA/TABLE/INDEX/VIEW
IF NOT EXISTS per SPEC §3.  NOTE: the usage_cost view DDL in SPEC §3 does
not parse as written — ``round(...) / 1e6, 6`` leaves the ``, 6`` outside
round().  ``_SCHEMA_DDL`` implements the intent: multiply token deltas by
per-1M-token prices, divide by 1e6 to get dollars, round to 6 decimals.  The
SPEC's cutover section records this deviation.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

_log = logging.getLogger("hivemind.telemetry.ledger")

# Bounded: a dead Postgres must not grow process memory.
_QUEUE_MAX = 10_000
_SHUTDOWN_TIMEOUT_S = 5.0

_INSERT_SQL = """
INSERT INTO mesh_telemetry.token_usage
    (agent_hash, provider, model, tokens_in, tokens_out, cache_read,
     cache_write, reasoning, latency_ms, status)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# Must stay in sync with the VALUES list above.
_COLUMN_ORDER = (
    "agent_hash",
    "provider",
    "model",
    "tokens_in",
    "tokens_out",
    "cache_read",
    "cache_write",
    "reasoning",
    "latency_ms",
    "status",
)

# One statement per execute (fake connections in tests record each call).
_SCHEMA_DDL = (
    "CREATE SCHEMA IF NOT EXISTS mesh_telemetry",
    """
    CREATE TABLE IF NOT EXISTS mesh_telemetry.token_usage (
        id           BIGSERIAL PRIMARY KEY,
        ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
        agent_hash   TEXT NOT NULL,          -- hivemind rate-limit bucket (already hashed)
        provider     TEXT NOT NULL,          -- observed (detect_provider profile name)
        model        TEXT NOT NULL,          -- observed from the request body
        tokens_in    BIGINT,                 -- all optional: providers vary
        tokens_out   BIGINT,
        cache_read   BIGINT,
        cache_write  BIGINT,
        reasoning    BIGINT,
        latency_ms   DOUBLE PRECISION,
        status       INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS token_usage_ts_idx ON mesh_telemetry.token_usage (ts)",
    """
    CREATE TABLE IF NOT EXISTS mesh_telemetry.model_pricing (
        provider          TEXT NOT NULL,
        model             TEXT NOT NULL,
        price_in          DOUBLE PRECISION,  -- USD per 1M tokens
        price_cache_read  DOUBLE PRECISION,
        price_cache_write DOUBLE PRECISION,
        price_out         DOUBLE PRECISION,
        PRIMARY KEY (provider, model)
    )
    """,
    # SPEC §3 DDL, with the syntax error fixed: round(SUM / 1e6, 6) — the
    # prices are per 1M tokens, so the dollar figure is the products divided
    # by 1e6, rounded to 6 decimals (microdollars).
    """
    CREATE OR REPLACE VIEW mesh_telemetry.usage_cost AS
    SELECT u.*,
           CASE WHEN p.provider IS NULL THEN NULL
                ELSE round((
                  (coalesce(u.tokens_in, 0) - coalesce(u.cache_write, 0) - coalesce(u.cache_read, 0))
                    * coalesce(p.price_in, 0)
                  + coalesce(u.cache_read, 0)  * coalesce(p.price_cache_read, 0)
                  + coalesce(u.cache_write, 0) * coalesce(p.price_cache_write, 0)
                  + coalesce(u.tokens_out, 0)  * coalesce(p.price_out, 0)
                ) / 1e6, 6)
           END AS cost_usd
    FROM mesh_telemetry.token_usage u
    LEFT JOIN mesh_telemetry.model_pricing p
      ON p.provider = u.provider AND p.model = u.model
    """,
)

# -- Dashboard reads (all parameterized on the day window) -----------------

_WINDOW = "ts >= now() - make_interval(days => %s)"

_SQL_DAILY = f"""
SELECT date_trunc('day', ts)::date AS day,
       provider,
       count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       round(coalesce(sum(cost_usd), 0), 6) AS cost_usd
FROM mesh_telemetry.usage_cost
WHERE {_WINDOW}
GROUP BY 1, 2
ORDER BY day ASC, provider ASC
"""

_SQL_TOTALS = f"""
SELECT count(*) AS requests,
       count(*) FILTER (WHERE status >= 400) AS errors,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       coalesce(sum(cost_usd), 0)::double precision AS cost_usd,
       count(*) FILTER (WHERE provider ILIKE '%ollama%') AS local_requests
FROM mesh_telemetry.usage_cost
WHERE {_WINDOW}
"""

_SQL_TOP_MODELS = f"""
SELECT provider, model, count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       round(coalesce(sum(cost_usd), 0), 6) AS cost_usd
FROM mesh_telemetry.usage_cost
WHERE {_WINDOW}
GROUP BY provider, model
HAVING coalesce(sum(cost_usd), 0) > 0
ORDER BY cost_usd DESC, requests DESC
LIMIT 10
"""

_SQL_AGENTS = f"""
SELECT agent_hash, count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       round(coalesce(sum(cost_usd), 0), 6) AS cost_usd,
       count(*) FILTER (WHERE status >= 400) AS errors
FROM mesh_telemetry.usage_cost
WHERE {_WINDOW}
GROUP BY agent_hash
ORDER BY cost_usd DESC, requests DESC
LIMIT 50
"""

_SQL_LATENCY = f"""
SELECT provider, count(*) AS requests,
       round(percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms), 1) AS p50_ms,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms), 1) AS p95_ms
FROM mesh_telemetry.token_usage
WHERE {_WINDOW}
  AND latency_ms IS NOT NULL
GROUP BY provider
ORDER BY p95_ms DESC NULLS LAST
"""


def _insert_params(row: dict[str, Any]) -> tuple:
    """Column-ordered parameter tuple; missing keys become NULL (usage is optional)."""
    return tuple(row.get(col) for col in _COLUMN_ORDER)


async def _default_connect(dsn: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row)


async def _safe_close(conn: Any) -> None:
    try:
        await conn.close()
    except Exception:
        pass


async def _fetch_all(conn: Any, sql: str, params: tuple) -> list[dict]:
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return list(rows or [])


async def _fetch_one(conn: Any, sql: str, params: tuple) -> dict | None:
    cursor = await conn.execute(sql, params)
    return await cursor.fetchone()


class TelemetryLedger:
    """Async, fail-open Postgres ledger + dashboard reader.

    One background task drains an internal queue; callers only ever enqueue
    (``record``) and never await Postgres.  Dashboard reads open their own
    short-lived connection so an unhealthy writer can never stall the page.
    Tests inject a fake connection factory (no real Postgres).
    """

    def __init__(
        self,
        dsn: str,
        *,
        conn_factory: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.dsn = dsn
        self._conn_factory = conn_factory or _default_connect
        self._conn: Any = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Best-effort connect + schema creation.  Never raises (D4)."""
        try:
            await self._connection()
        except Exception:
            _log.debug("telemetry connect failed (fail-open; next write retries)", exc_info=True)

    async def shutdown(self) -> None:
        """Drain queued rows (bounded wait), stop the worker, close.  Idempotent; never raises."""
        if self._closed and self._worker is None:
            return
        self._closed = True
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            try:
                self._queue.put_nowait(None)  # sentinel: drain then exit
            except (asyncio.QueueFull, AttributeError):
                pass
            try:
                await asyncio.wait_for(worker, timeout=_SHUTDOWN_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                worker.cancel()
                try:
                    await worker
                except (asyncio.CancelledError, Exception):
                    pass
        await self._reset_connection()

    # -- write path --------------------------------------------------------

    async def record(self, row: dict[str, Any]) -> None:
        """Schedule one usage row for INSERT.  Never raises; never does I/O."""
        if self._closed:
            return
        try:
            self._ensure_worker()
            self._queue.put_nowait(dict(row))
        except asyncio.QueueFull:
            _log.debug("telemetry queue full; dropping row (fail-open)")
        except Exception:
            _log.debug("telemetry record scheduling failed (fail-open)", exc_info=True)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._queue = asyncio.Queue(maxsize=_QUEUE_MAX)
            self._worker = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        while True:
            row = await self._queue.get()
            try:
                if row is None:
                    return
                await self._write_row(row)
            except Exception:
                _log.debug("telemetry write failed (fail-open)", exc_info=True)
            finally:
                self._queue.task_done()

    async def _write_row(self, row: dict[str, Any]) -> None:
        conn = await self._connection()  # lazy reconnect: retried on next write
        try:
            await conn.execute(_INSERT_SQL, _insert_params(row))
        except Exception:
            await self._reset_connection()
            raise

    async def _connection(self) -> Any:
        if self._conn is None:
            conn = await self._conn_factory(self.dsn)
            try:
                await conn.set_autocommit(True)
                await self._ensure_schema(conn)
            except Exception:
                await _safe_close(conn)
                raise
            self._conn = conn
        return self._conn

    async def _reset_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            await _safe_close(conn)

    async def _ensure_schema(self, conn: Any) -> None:
        for statement in _SCHEMA_DDL:
            await conn.execute(statement)

    # -- read path (dashboard) ----------------------------------------------

    async def fetch_dashboard(self, days: int = 14) -> dict[str, Any]:
        """Aggregated usage/cost for the dashboard page.

        Raises on DB failure — the HTTP handler maps failures to the
        ``telemetry unavailable`` payload.  ``days`` is parameterized into
        every query.
        """
        conn = await self._conn_factory(self.dsn)
        try:
            await conn.set_autocommit(True)
            await self._ensure_schema(conn)  # self-healing: view may not exist yet
            daily = await _fetch_all(conn, _SQL_DAILY, (days,))
            totals = await _fetch_one(conn, _SQL_TOTALS, (days,))
            top_models = await _fetch_all(conn, _SQL_TOP_MODELS, (days,))
            agents = await _fetch_all(conn, _SQL_AGENTS, (days,))
            latency = await _fetch_all(conn, _SQL_LATENCY, (days,))
        finally:
            await _safe_close(conn)
        return _shape_dashboard(days, totals, daily, top_models, agents, latency)


class NullLedger:
    """No-op ledger used when no DSN is configured (zero behavior change)."""

    async def record(self, row: dict[str, Any]) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def fetch_dashboard(self, days: int = 14) -> dict[str, Any]:
        return {"error": "telemetry unavailable"}


# -- module-level singleton --------------------------------------------------
# The interceptor calls get_ledger() at request time, so the wiring can swap
# the real ledger in and out without touching the proxy hot path.

_ledger: TelemetryLedger | NullLedger = NullLedger()


def set_ledger(ledger: TelemetryLedger | NullLedger) -> None:
    """Install the process-wide ledger (server wiring; tests)."""
    global _ledger
    _ledger = ledger


def get_ledger() -> TelemetryLedger | NullLedger:
    """The active ledger — a no-op NullLedger when the DSN is unset."""
    return _ledger


def _shape_dashboard(
    days: int,
    totals: dict | None,
    daily: list[dict],
    top_models: list[dict],
    agents: list[dict],
    latency: list[dict],
) -> dict[str, Any]:
    """Normalize dict_row output into a JSON-safe payload (dates, Decimal-free)."""
    requests = int((totals or {}).get("requests") or 0)
    errors = int((totals or {}).get("errors") or 0)
    local_requests = int((totals or {}).get("local_requests") or 0)

    def _cost(value: Any) -> float:
        return 0.0 if value is None else float(value)

    shaped_agents = []
    for row in agents:
        agent_requests = int(row["requests"])
        agent_errors = int(row["errors"])
        shaped_agents.append(
            {
                "agent_hash": row["agent_hash"],
                "requests": agent_requests,
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "cost_usd": _cost(row["cost_usd"]),
                "errors": agent_errors,
                "error_rate": round(agent_errors / agent_requests, 4) if agent_requests else 0.0,
            }
        )

    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "requests": requests,
            "errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0.0,
            "tokens_in": int((totals or {}).get("tokens_in") or 0),
            "tokens_out": int((totals or {}).get("tokens_out") or 0),
            "cost_usd": _cost((totals or {}).get("cost_usd")),
            "local_requests": local_requests,
            "local_share_pct": round(local_requests / requests * 100.0, 2) if requests else 0.0,
        },
        "daily": [
            {
                "day": str(row["day"]),
                "provider": row["provider"],
                "requests": int(row["requests"]),
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "cost_usd": _cost(row["cost_usd"]),
            }
            for row in daily
        ],
        "top_models": [
            {
                "provider": row["provider"],
                "model": row["model"],
                "requests": int(row["requests"]),
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "cost_usd": _cost(row["cost_usd"]),
            }
            for row in top_models
        ],
        "agents": shaped_agents,
        "latency": [
            {
                "provider": row["provider"],
                "requests": int(row["requests"]),
                "p50_ms": None if row["p50_ms"] is None else float(row["p50_ms"]),
                "p95_ms": None if row["p95_ms"] is None else float(row["p95_ms"]),
            }
            for row in latency
        ],
    }
