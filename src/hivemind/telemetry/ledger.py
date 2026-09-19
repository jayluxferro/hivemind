"""Fire-and-forget token-usage ledger (Postgres) + analytics reads.

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

Reads are the opposite of writes: each one opens its own short-lived
connection and lets failures propagate, because the HTTP handlers map a raise
to the ``{"error": "telemetry unavailable"}`` payload with HTTP 200 and the
page renders its error state.  Reads take an inclusive display range of
aware-UTC datetimes and convert it to the half-open SQL window ``ts >= %s AND
ts < %s`` (SPEC D7); all day/hour bucketing happens ``AT TIME ZONE 'UTC'`` so
a label never shifts with the server's timezone.

Retention (D10): a background pruner deletes rows older than
``retention_days`` once on connect and then every 24h.  It is fail-open and
cancelled in ``shutdown()`` before the writer connection is reset.

Token semantics: ``tokens_in`` is the FRESH (uncached) input portion on
DeepSeek's Anthropic-compatible shim — cache reads are reported in
``cache_read`` and excluded from ``tokens_in``.  Real input is therefore
``cache_read + tokens_in``, and the cache-hit share is
``cache_read / (cache_read + tokens_in)`` (exposed as ``cache_hit_pct``
in the dashboard payload).  ``conversation_hash`` is the sha256-truncated
client session header, so new-session starts are distinguishable from
mid-session cache misses in analysis.

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
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

_log = logging.getLogger("hivemind.telemetry.ledger")

# Bounded: a dead Postgres must not grow process memory.
_QUEUE_MAX = 10_000
_SHUTDOWN_TIMEOUT_S = 5.0
#: How often the pruner runs (after its immediate first pass).
_PRUNE_INTERVAL_S = 24 * 60 * 60
#: Rows kept by default when no retention is configured (SPEC D10).
DEFAULT_RETENTION_DAYS = 90

_INSERT_SQL = """
INSERT INTO mesh_telemetry.token_usage
    (agent_hash, provider, model, tokens_in, tokens_out, cache_read,
     cache_write, reasoning, latency_ms, status, conversation_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
    "conversation_hash",
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
        tokens_in    BIGINT,                 -- FRESH input tokens: DeepSeek's Anthropic shim
                                             -- reports cache reads separately, so tokens_in
                                             -- excludes them; other providers vary (see module
                                             -- docstring)
        tokens_out   BIGINT,
        cache_read   BIGINT,
        cache_write  BIGINT,
        reasoning    BIGINT,
        latency_ms   DOUBLE PRECISION,
        status       INTEGER NOT NULL,
        conversation_hash TEXT               -- sha256[:16] of the client session header
                                             -- (x-claude-code-session-id et al); NULL = none sent
    )
    """,
    "CREATE INDEX IF NOT EXISTS token_usage_ts_idx ON mesh_telemetry.token_usage (ts)",
    # Existing databases predate the column; CREATE TABLE IF NOT EXISTS does
    # not touch them, so this ALTER is the migration.
    "ALTER TABLE mesh_telemetry.token_usage ADD COLUMN IF NOT EXISTS conversation_hash TEXT",
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
    # Migration guard: the view is SELECT u.*, so it freezes the table's
    # column list at creation time.  When the ALTER above adds a column,
    # CREATE OR REPLACE cannot reconcile the old shape — PG refuses to
    # rename a view column (measured: "cannot change name of view column
    # cost_usd to conversation_hash").  Drop the view exactly when the
    # shapes mismatch (cheap catalog count, no-op afterwards); the
    # REPLACE below then recreates it with the new shape.
    """
    DO $$ BEGIN
      IF (SELECT count(*) FROM information_schema.columns
           WHERE table_schema = 'mesh_telemetry' AND table_name = 'usage_cost')
         <> (SELECT count(*) FROM information_schema.columns
              WHERE table_schema = 'mesh_telemetry' AND table_name = 'token_usage') + 1
      THEN
        EXECUTE 'DROP VIEW IF EXISTS mesh_telemetry.usage_cost';
      END IF;
    END $$;
    """,
    # SPEC §3 DDL, with the syntax error fixed: round(SUM / 1e6 AS numeric), 6) — the
    # prices are per 1M tokens, so the dollar figure is the products divided
    # by 1e6, rounded to 6 decimals (microdollars).
    """
    CREATE OR REPLACE VIEW mesh_telemetry.usage_cost AS
    SELECT u.*,
           CASE WHEN p.provider IS NULL THEN NULL
                ELSE round(CAST((
                  (coalesce(u.tokens_in, 0) - coalesce(u.cache_write, 0) - coalesce(u.cache_read, 0))
                    * coalesce(p.price_in, 0)
                  + coalesce(u.cache_read, 0)  * coalesce(p.price_cache_read, 0)
                  + coalesce(u.cache_write, 0) * coalesce(p.price_cache_write, 0)
                  + coalesce(u.tokens_out, 0)  * coalesce(p.price_out, 0)
                ) / 1e6 AS numeric), 6)
           END AS cost_usd
    FROM mesh_telemetry.token_usage u
    LEFT JOIN mesh_telemetry.model_pricing p
      ON p.provider = u.provider AND p.model = u.model
    """,
)

# -- Reads (all parameterized on the display range) -------------------------
#
# The display range is inclusive [from_ts, to_ts]; SQL wants a half-open
# window, so every query below matches on _RANGE and the ledger converts the
# bound once (_range_params).  `ts AT TIME ZONE 'UTC'` on every bucket label:
# a timestamptz bucketed in the server's local zone would move rows across
# day boundaries depending on where Postgres happens to run.

_RANGE = "ts >= %s AND ts < %s"

# Daily usage per agent — the primary overview dimension.  The provider
# dimension is deliberately NOT charted: for proxy-in-a-pipeline deployments
# every upstream detects as the same profile ("Generic"), so it carries no
# signal.  Top-5 agents by window tokens get their own series; the rest fold
# into "Other" in SQL so the payload stays small regardless of agent count.
#
# The window predicate lives in the `filtered` CTE alone: ranking and
# aggregation both read it, so the parameter is bound exactly once (Phase 1
# carried it twice — once per mention — which the old tests pinned as a wart).
_SQL_DAILY_AGENTS = f"""
WITH filtered AS (
    SELECT * FROM mesh_telemetry.token_usage
    WHERE {_RANGE}
),
ranked AS (
    SELECT agent_hash,
           sum(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)) AS window_tokens
    FROM filtered
    GROUP BY agent_hash
    ORDER BY window_tokens DESC
    LIMIT 5
)
SELECT date_trunc('day', f.ts AT TIME ZONE 'UTC')::date AS day,
       CASE WHEN r.agent_hash IS NULL THEN 'Other' ELSE f.agent_hash END AS agent_hash,
       count(*) AS requests,
       sum(coalesce(f.tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(f.tokens_out, 0))::bigint AS tokens_out,
       count(*) FILTER (WHERE f.status >= 400) AS errors
FROM filtered f
LEFT JOIN ranked r ON r.agent_hash = f.agent_hash
GROUP BY 1, 2
ORDER BY day ASC, agent_hash ASC
"""

_SQL_TOTALS = f"""
SELECT count(*) AS requests,
       count(*) FILTER (WHERE status >= 400) AS errors,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       sum(coalesce(cache_read, 0))::bigint AS cache_read,
       sum(coalesce(cache_write, 0))::bigint AS cache_write,
       -- tokens_in is fresh-only on the dominant provider, so the real input
       -- is cache_read + tokens_in and the ratio is the cache-hit share.
       -- 0-1 scale, matching error_rate: the payload carries ONE scale so
       -- a UI formatter can never multiply the wrong field by 100 (the
       -- 8450-tile bug: a 0-100 value through the 0-1 percent formatter;
       -- no bare percent signs in SQL text -- psycopg scans comments too).
       round(sum(coalesce(cache_read, 0))::numeric /
             nullif(sum(coalesce(cache_read, 0)) + sum(coalesce(tokens_in, 0)), 0), 4)
             AS cache_hit_pct,
       coalesce(sum(cost_usd), 0)::double precision AS cost_usd
FROM mesh_telemetry.usage_cost
WHERE {_RANGE}
"""

# Ranked by tokens, not cost — token usage is the primary signal; cost is a
# secondary column that only populates when a pricing row exists.
_SQL_TOP_MODELS = f"""
SELECT provider, model, count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       sum(coalesce(cache_read, 0))::bigint AS cache_read,
       round(sum(coalesce(cache_read, 0))::numeric /
             nullif(sum(coalesce(cache_read, 0)) + sum(coalesce(tokens_in, 0)), 0), 4)
             AS cache_hit_pct,
       round(coalesce(sum(cost_usd), 0), 6) AS cost_usd
FROM mesh_telemetry.usage_cost
WHERE {_RANGE}
GROUP BY provider, model
ORDER BY (sum(coalesce(tokens_in, 0)) + sum(coalesce(tokens_out, 0))) DESC, requests DESC
LIMIT 10
"""

_SQL_AGENTS = f"""
SELECT agent_hash, count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       sum(coalesce(cache_read, 0))::bigint AS cache_read,
       sum(coalesce(cache_write, 0))::bigint AS cache_write,
       round(coalesce(sum(cost_usd), 0), 6) AS cost_usd,
       count(*) FILTER (WHERE status >= 400) AS errors
FROM mesh_telemetry.usage_cost
WHERE {_RANGE}
GROUP BY agent_hash
ORDER BY (sum(coalesce(tokens_in, 0)) + sum(coalesce(tokens_out, 0))) DESC, requests DESC
LIMIT 50
"""

_SQL_LATENCY = f"""
SELECT provider, count(*) AS requests,
       round((percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p50_ms,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p95_ms
FROM mesh_telemetry.token_usage
WHERE {_RANGE}
  AND latency_ms IS NOT NULL
GROUP BY provider
ORDER BY p95_ms DESC NULLS LAST
"""

# Latency per provider+model: the provider rollup above hides which model is
# slow, and model is the axis an operator can actually act on.
_SQL_LATENCY_MODELS = f"""
SELECT provider, model, count(*) AS requests,
       round((percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p50_ms,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p95_ms
FROM mesh_telemetry.token_usage
WHERE {_RANGE}
  AND latency_ms IS NOT NULL
GROUP BY provider, model
ORDER BY p95_ms DESC NULLS LAST, requests DESC
LIMIT 15
"""

# Status distribution: bucketed by the *stored* status, so the overview can
# show 4xx vs 5xx vs 429 without another endpoint.
_SQL_STATUS = f"""
SELECT status, count(*) AS requests
FROM mesh_telemetry.token_usage
WHERE {_RANGE}
GROUP BY status
ORDER BY requests DESC
LIMIT 12
"""

# -- Filter facets (drill-down pickers): distinct values + counts -----------

_SQL_FACET_AGENTS = f"""
SELECT agent_hash, count(*) AS requests
FROM mesh_telemetry.token_usage
WHERE {_RANGE}
GROUP BY agent_hash
ORDER BY requests DESC
LIMIT 200
"""

_SQL_FACET_MODELS = f"""
SELECT provider, model, count(*) AS requests
FROM mesh_telemetry.token_usage
WHERE {_RANGE}
GROUP BY provider, model
ORDER BY requests DESC
LIMIT 100
"""

_SQL_FACET_PROVIDERS = f"""
SELECT provider, count(*) AS requests
FROM mesh_telemetry.token_usage
WHERE {_RANGE}
GROUP BY provider
ORDER BY requests DESC
"""

# -- Series (drill-down activity over time) --------------------------------
#
# One SELECT, two bucket sizes; the WHERE body is filled in at call time by
# _build_request_where so the optional filters append in _REQUEST_FILTERS
# order.  With no filters the queries run exactly as written here.

_SQL_SERIES_HOUR = """
SELECT date_trunc('hour', ts AT TIME ZONE 'UTC') AS bucket_start,
       count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       sum(coalesce(cache_read, 0))::bigint AS cache_read,
       count(*) FILTER (WHERE status >= 400) AS errors
FROM mesh_telemetry.token_usage
WHERE {where}
GROUP BY 1
ORDER BY 1 ASC
"""

_SQL_SERIES_DAY = """
SELECT date_trunc('day', ts AT TIME ZONE 'UTC') AS bucket_start,
       count(*) AS requests,
       sum(coalesce(tokens_in, 0))::bigint AS tokens_in,
       sum(coalesce(tokens_out, 0))::bigint AS tokens_out,
       sum(coalesce(cache_read, 0))::bigint AS cache_read,
       count(*) FILTER (WHERE status >= 400) AS errors
FROM mesh_telemetry.token_usage
WHERE {where}
GROUP BY 1
ORDER BY 1 ASC
"""

_SERIES_SQL = {"hour": _SQL_SERIES_HOUR, "day": _SQL_SERIES_DAY}

# -- Request-level rows (drill-down + export) -------------------------------

# The single source of truth for the row shape: the SELECT list, the CSV
# header, the JSONL key order and the API's `rows` objects all come from here.
_REQUEST_COLUMNS = (
    "id",
    "ts",
    "agent_hash",
    "provider",
    "model",
    "tokens_in",
    "tokens_out",
    "cache_read",
    "cache_write",
    "latency_ms",
    "status",
)

_COLUMNS_SQL = ", ".join(_REQUEST_COLUMNS)

# Exact-match filters, in the order their placeholders appear.  The order is
# the contract (SPEC D8): (from, to), then these, then limit/offset.
_REQUEST_FILTERS = (
    ("agent_hash", "agent_hash = %s"),
    ("model", "model = %s"),
    ("provider", "provider = %s"),
    ("status", "status = %s"),
)

# Whitelists: a query-string value is only ever used as a dict *key* here, so
# an unknown value falls back to the default instead of reaching SQL.
_REQUEST_SORTS = {
    "ts": "ts",
    "tokens": "(coalesce(tokens_in, 0) + coalesce(tokens_out, 0))",
    "latency": "latency_ms",
    "status": "status",
    "agent": "agent_hash",
    "model": "model",
}
_REQUEST_ORDERS = {"asc": "ASC", "desc": "DESC"}
_DEFAULT_SORT = "ts"
_DEFAULT_ORDER = "desc"

# `{columns}`/`{where}`/`{order_by}` are filled by _requests_sql below — never
# by user input: `where` comes from _build_request_where (which only
# interpolates whitelisted column names) and `order_by` from _order_by.
_SQL_REQUESTS = """
SELECT {columns}
FROM mesh_telemetry.token_usage
WHERE {where}
ORDER BY {order_by}
LIMIT %s OFFSET %s
"""

_SQL_REQUESTS_COUNT = """
SELECT count(*) AS total
FROM mesh_telemetry.token_usage
WHERE {where}
"""

# Export streams ordered rows and carries no LIMIT/OFFSET of its own.
_SQL_REQUESTS_EXPORT = """
SELECT {columns}
FROM mesh_telemetry.token_usage
WHERE {where}
ORDER BY {order_by}
"""

# Retention: the pruner's only statement.  make_interval keeps the unit in
# SQL, so the parameter stays a plain integer (SPEC D10).
_SQL_PRUNE = "DELETE FROM mesh_telemetry.token_usage WHERE ts < now() - make_interval(days => %s)"


def _build_request_where(
    from_ts: datetime,
    to_ts: datetime,
    filters: Mapping[str, Any] | None = None,
) -> tuple[str, tuple[Any, ...]]:
    """WHERE body + params for one request-level read.

    The range clause always leads, then the active filters in
    ``_REQUEST_FILTERS`` order, so the placeholder order is exactly
    ``(from, to), filters…`` (SPEC D8).  Callers append their own trailing
    placeholders (limit/offset) to the returned params.  Only whitelisted
    column names are interpolated; every value stays a bound parameter.

    ``to_ts`` arrives *inclusive* (what a date picker shows, same contract as
    ``fetch_overview``) and leaves half-open via :func:`_range_params`, so no
    caller has to remember the ``+ 1 day``.
    """
    from_ts, to_ts = _range_params(from_ts, to_ts)
    clauses = [_RANGE]
    params: list[Any] = [from_ts, to_ts]
    active = filters or {}
    for name, clause in _REQUEST_FILTERS:
        value = active.get(name)
        if value is None or value == "":
            continue
        clauses.append(clause)
        params.append(value)
    return " AND ".join(clauses), tuple(params)


def _order_by(sort: str, order: str) -> str:
    """Whitelisted ORDER BY body (user input is only ever a dict key).

    ``NULLS LAST`` in both directions: ``latency_ms`` is nullable, and
    "slowest first" should not mean "rows that never reported a latency"
    (Postgres sorts NULLs first on DESC).  ``id`` breaks ties so paging is
    stable across identical timestamps.
    """
    column = _REQUEST_SORTS.get(str(sort or "").lower(), _REQUEST_SORTS[_DEFAULT_SORT])
    direction = _REQUEST_ORDERS.get(str(order or "").lower(), _REQUEST_ORDERS[_DEFAULT_ORDER])
    return f"{column} {direction} NULLS LAST, id {direction}"


def _range_params(from_ts: datetime, to_ts: datetime) -> tuple[datetime, datetime]:
    """Inclusive display range -> the half-open window parameters.

    Display dates are inclusive (that is what a date picker shows); SQL wants
    ``ts < to + 1 day``.  The conversion lives here — once — so handlers can
    stay dumb about it.  Imported locally because ``query`` imports the
    whitelists above (a module-level import would be circular).
    """
    from .query import exclusive_end

    return from_ts, exclusive_end(to_ts)


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
    """Async, fail-open Postgres ledger + analytics reader.

    One background task drains an internal queue; callers only ever enqueue
    (``record``) and never await Postgres.  Reads open their own short-lived
    connection so an unhealthy writer can never stall a page; the pruner does
    the same.  Tests inject a fake connection factory (no real Postgres).
    """

    def __init__(
        self,
        dsn: str,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        conn_factory: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.dsn = dsn
        # Floor at one day: a 0/negative retention turns the pruner into
        # "delete everything".  normalize_runtime_limits() already rejects
        # such a config loudly; this is the last line of defence for callers
        # that build a ledger directly.
        self.retention_days = max(1, int(retention_days))
        self._conn_factory = conn_factory or _default_connect
        self._conn: Any = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._pruner: asyncio.Task | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        """Best-effort connect + schema creation.  Never raises (D4).

        The pruner only starts once a connection actually succeeded: with no
        schema there is nothing to prune, and a failed connect must stay as
        quiet as it did before retention existed.
        """
        try:
            await self._connection()
        except Exception:
            _log.debug("telemetry connect failed (fail-open; next write retries)", exc_info=True)
            return
        self._start_pruner()

    async def shutdown(self) -> None:
        """Stop the pruner, drain queued rows (bounded wait), stop the worker, close.

        Idempotent; never raises.  Order matters: the pruner is cancelled
        *before* the writer connection is reset, so it can never fire a DELETE
        against a connection this method is about to close.
        """
        if self._closed and self._worker is None:
            return
        self._closed = True
        pruner, self._pruner = self._pruner, None
        if pruner is not None and not pruner.done():
            pruner.cancel()
            try:
                await asyncio.wait_for(pruner, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
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

    # -- retention (prune) --------------------------------------------------

    async def prune(self) -> None:
        """Delete rows older than ``retention_days``.  Never raises (D10).

        Opens its own short-lived connection (never the writer's) so a slow
        DELETE cannot sit in front of queued INSERTs.  Cancellation is the one
        exception that propagates — that is how ``shutdown()`` stops it.
        """
        try:
            conn = await self._conn_factory(self.dsn)
            try:
                await conn.set_autocommit(True)
                await conn.execute(_SQL_PRUNE, (self.retention_days,))
            finally:
                await _safe_close(conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.debug("telemetry prune failed (fail-open)", exc_info=True)

    def _start_pruner(self) -> None:
        if self._pruner is None or self._pruner.done():
            self._pruner = asyncio.create_task(self._prune_loop())

    async def _prune_loop(self) -> None:
        """Prune immediately, then once every ``_PRUNE_INTERVAL_S``.

        Immediate first pass: a restart must not leave an over-retention table
        sitting there for 24h.  ``prune()`` swallows its own failures, so the
        only way out of this loop is cancellation.  The delete sits *inside*
        the loop — with it above the `while`, retention would only ever run
        once per process start.
        """
        while not self._closed:
            await self.prune()
            await asyncio.sleep(_PRUNE_INTERVAL_S)

    # -- read path ----------------------------------------------------------
    #
    # Every read follows the same shape: a short-lived connection (autocommit,
    # self-healing schema) closed in `finally`, with failures left to
    # propagate — the HTTP handler maps a raise to the "telemetry unavailable"
    # payload with HTTP 200 (D4), so the ledger must NOT swallow here or the
    # page would render an empty dashboard as if there were no traffic.

    @asynccontextmanager
    async def _read_conn(self) -> AsyncIterator[Any]:
        conn = await self._conn_factory(self.dsn)
        try:
            await conn.set_autocommit(True)
            await self._ensure_schema(conn)  # self-healing: view may not exist yet
            yield conn
        finally:
            await _safe_close(conn)

    async def fetch_overview(self, from_ts: datetime, to_ts: datetime) -> dict[str, Any]:
        """Aggregated usage for the overview view over an inclusive range.

        Seven queries, all on the same half-open window.  Raises on DB failure
        (see above).
        """
        params = _range_params(from_ts, to_ts)
        async with self._read_conn() as conn:
            daily_agents = await _fetch_all(conn, _SQL_DAILY_AGENTS, params)
            totals = await _fetch_one(conn, _SQL_TOTALS, params)
            top_models = await _fetch_all(conn, _SQL_TOP_MODELS, params)
            agents = await _fetch_all(conn, _SQL_AGENTS, params)
            latency = await _fetch_all(conn, _SQL_LATENCY, params)
            status_codes = await _fetch_all(conn, _SQL_STATUS, params)
            latency_models = await _fetch_all(conn, _SQL_LATENCY_MODELS, params)
        return _shape_overview(
            from_ts,
            to_ts,
            totals,
            daily_agents,
            top_models,
            agents,
            latency,
            status_codes,
            latency_models,
        )

    async def fetch_dashboard(self, days: int = 14) -> dict[str, Any]:
        """Deprecated Phase 1 alias: ``days`` -> :meth:`fetch_overview`.

        Kept for out-of-tree callers and older tests; the payload is the
        overview shape (``days`` itself is gone from it — every read is now a
        range, and the frontend always knows its own window).
        """
        now = datetime.now(timezone.utc)
        days = max(1, min(365, int(days)))
        return await self.fetch_overview(now - timedelta(days=days), now)

    async def fetch_requests(
        self,
        from_ts: datetime,
        to_ts: datetime,
        *,
        agent_hash: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        status: int | None = None,
        sort: str = _DEFAULT_SORT,
        order: str = _DEFAULT_ORDER,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of raw request rows (metadata only — SPEC D5/D8).

        Filters are exact-match; ``sort``/``order`` resolve through the
        whitelists above.  The total is counted before the page is fetched so
        the caller can render "N of M" without a second round trip.
        """
        where, params = _build_request_where(
            from_ts,
            to_ts,
            {"agent_hash": agent_hash, "model": model, "provider": provider, "status": status},
        )
        # Lower bounds only: the upper clamps live in query.parse_request_filters
        # (single validation point), but a negative LIMIT/OFFSET is a SQL error,
        # not a clamp, so the ledger refuses to pass one through.
        page_size = max(1, int(limit))
        start = max(0, int(offset))
        async with self._read_conn() as conn:
            total_row = await _fetch_one(conn, _SQL_REQUESTS_COUNT.format(where=where), params)
            rows = await _fetch_all(
                conn,
                _SQL_REQUESTS.format(columns=_COLUMNS_SQL, where=where, order_by=_order_by(sort, order)),
                params + (page_size, start),
            )
        return {
            "total": int((total_row or {}).get("total") or 0),
            "limit": page_size,
            "offset": start,
            "from": _iso_z(from_ts),
            "to": _iso_z(to_ts),
            "rows": [_shape_row(row) for row in rows],
        }

    async def fetch_facets(self, from_ts: datetime, to_ts: datetime) -> dict[str, Any]:
        """Distinct agent/model/provider values with counts, busiest first.

        Feeds the drill-down pickers: the lists are capped in SQL (200/100)
        and ordered by request count so a truncated list keeps the values that
        matter.
        """
        params = _range_params(from_ts, to_ts)
        async with self._read_conn() as conn:
            agents = await _fetch_all(conn, _SQL_FACET_AGENTS, params)
            models = await _fetch_all(conn, _SQL_FACET_MODELS, params)
            providers = await _fetch_all(conn, _SQL_FACET_PROVIDERS, params)
        return {
            "agents": [{"agent_hash": row["agent_hash"], "requests": int(row["requests"])} for row in agents],
            "models": [
                {"provider": row["provider"], "model": row["model"], "requests": int(row["requests"])} for row in models
            ],
            "providers": [{"provider": row["provider"], "requests": int(row["requests"])} for row in providers],
        }

    async def fetch_series(
        self,
        from_ts: datetime,
        to_ts: datetime,
        bucket: str,
        *,
        agent_hash: str | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Activity over time, bucketed by hour or day (ascending).

        The bucket is a dict key, never a SQL fragment: an unknown value is a
        ``ValueError`` for the caller (the handler clamps to a known bucket
        before calling), not an interpolated string.
        """
        template = _SERIES_SQL.get(bucket)
        if template is None:
            raise ValueError(f"invalid bucket {bucket!r}; expected one of {tuple(_SERIES_SQL)}")
        where, params = _build_request_where(
            from_ts, to_ts, {"agent_hash": agent_hash, "model": model, "provider": provider}
        )
        async with self._read_conn() as conn:
            rows = await _fetch_all(conn, template.format(where=where), params)
        return {"bucket": bucket, "rows": [_shape_series_row(row) for row in rows]}

    async def count_requests(
        self,
        from_ts: datetime,
        to_ts: datetime,
        *,
        filters: Mapping[str, Any] | None = None,
    ) -> int:
        """How many rows the same filters would return (export pre-flight)."""
        where, params = _build_request_where(from_ts, to_ts, filters)
        async with self._read_conn() as conn:
            row = await _fetch_one(conn, _SQL_REQUESTS_COUNT.format(where=where), params)
        return int((row or {}).get("total") or 0)

    async def export_rows(
        self,
        from_ts: datetime,
        to_ts: datetime,
        *,
        filters: Mapping[str, Any] | None = None,
        batch_size: int = 1000,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Stream shaped rows in ``batch_size`` batches (SPEC D9).

        A named server-side cursor keeps the result set in Postgres instead of
        in the proxy's memory; each yielded batch is already JSON-safe.  The
        caller has committed to a status code by the time this runs (the
        pre-flight count), so a mid-stream failure is a documented truncation:
        the cursor and connection are closed here and the exception surfaces
        in the streaming body, which logs it at DEBUG and ends the stream.
        """
        where, params = _build_request_where(from_ts, to_ts, filters)
        sql = _SQL_REQUESTS_EXPORT.format(
            columns=_COLUMNS_SQL, where=where, order_by=_order_by(_DEFAULT_SORT, _DEFAULT_ORDER)
        )
        conn = await self._conn_factory(self.dsn)
        cursor: Any = None
        try:
            await conn.set_autocommit(True)
            await self._ensure_schema(conn)
            # withhold=True: an autocommit connection is outside a transaction
            # block, and DECLARE CURSOR there is rejected unless the cursor is
            # WITH HOLD (psycopg 3.3 + Postgres: "DECLARE CURSOR can only be
            # used in transaction blocks").  The trade is real — WITH HOLD
            # materializes the result server-side — but it keeps the export
            # from holding a snapshot open for the whole download.
            cursor = conn.cursor(name="telemetry_export", withhold=True)
            await cursor.execute(sql, params)
            while True:
                batch = await cursor.fetchmany(batch_size)
                if not batch:
                    break
                yield [_shape_row(row) for row in batch]
        finally:
            if cursor is not None:
                try:
                    await cursor.close()
                except Exception:
                    pass
            await _safe_close(conn)


class NullLedger:
    """No-op ledger used when no DSN is configured (zero behavior change).

    Every reader answers the marker payload the routes hand straight to the
    page, so an unconfigured deployment shows "telemetry unavailable" instead
    of a 500 or an all-zeros dashboard.  ``count_requests``/``export_rows``
    keep their real types: the export handler iterates them, and an empty
    export is the honest answer when there is no ledger behind it.
    """

    async def record(self, row: dict[str, Any]) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def prune(self) -> None:
        return None

    async def fetch_overview(self, from_ts: datetime, to_ts: datetime) -> dict[str, Any]:
        return _unavailable()

    async def fetch_facets(self, from_ts: datetime, to_ts: datetime) -> dict[str, Any]:
        return _unavailable()

    async def fetch_series(
        self,
        from_ts: datetime,
        to_ts: datetime,
        bucket: str,
        *,
        agent_hash: str | None = None,
        model: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        return _unavailable()

    async def fetch_requests(self, from_ts: datetime, to_ts: datetime, **kwargs: Any) -> dict[str, Any]:
        return _unavailable()

    async def count_requests(
        self,
        from_ts: datetime,
        to_ts: datetime,
        *,
        filters: Mapping[str, Any] | None = None,
    ) -> int:
        return 0

    async def export_rows(
        self,
        from_ts: datetime,
        to_ts: datetime,
        *,
        filters: Mapping[str, Any] | None = None,
        batch_size: int = 1000,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        return
        yield  # pragma: no cover - empty async generator, never yields

    async def fetch_dashboard(self, days: int = 14) -> dict[str, Any]:
        """Deprecated Phase 1 alias for :meth:`fetch_overview`."""
        return _unavailable()


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


def _unavailable() -> dict[str, str]:
    """The fail-open marker every route maps to HTTP 200 (SPEC D4)."""
    return {"error": "telemetry unavailable"}


def _iso_z(value: Any) -> str | None:
    """Timestamp -> ISO 8601 with a ``Z`` suffix (naive values read as UTC).

    Dates (``date_trunc(...)::date``) stringify to ``YYYY-MM-DD``; the JS side
    parses day labels as text precisely so no timezone can shift them.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return str(value)


def _opt_int(value: Any) -> int | None:
    """Nullable counter -> int or None.  Usage columns are optional (D2), and
    "not reported" must stay distinguishable from "zero"."""
    return None if value is None else int(value)


def _shape_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One token_usage row as JSON-safe primitives (SPEC D8 row shape).

    Keys mirror :data:`_REQUEST_COLUMNS` so the JSONL export and the API rows
    are the same objects; Latency stays a float (percentiles need the
    precision) and every counter is an int-or-null.
    """
    return {
        "id": int(row["id"]),
        "ts": _iso_z(row.get("ts")),
        "agent_hash": row.get("agent_hash"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "tokens_in": _opt_int(row.get("tokens_in")),
        "tokens_out": _opt_int(row.get("tokens_out")),
        "cache_read": _opt_int(row.get("cache_read")),
        "cache_write": _opt_int(row.get("cache_write")),
        "latency_ms": None if row.get("latency_ms") is None else float(row["latency_ms"]),
        "status": int(row["status"]),
    }


def _shape_series_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One bucketed activity row (bucket_start is a naive UTC timestamp)."""
    return {
        "bucket_start": _iso_z(row.get("bucket_start")),
        "requests": int(row["requests"]),
        "tokens_in": _opt_int(row.get("tokens_in")),
        "tokens_out": _opt_int(row.get("tokens_out")),
        "cache_read": _opt_int(row.get("cache_read")),
        "errors": int(row.get("errors") or 0),
    }


def _shape_overview(
    from_ts: datetime,
    to_ts: datetime,
    totals: dict | None,
    daily_agents: list[dict],
    top_models: list[dict],
    agents: list[dict],
    latency: list[dict],
    status_codes: list[dict],
    latency_models: list[dict],
) -> dict[str, Any]:
    """Normalize dict_row output into a JSON-safe payload (dates, Decimal-free).

    ``cost_usd`` survives here (Phase 1 payload shape) even though cost is
    excluded from the request rows and the export — the `usage_cost` view is
    there for opt-in consumers, and the UI no longer shows dollars.
    """
    requests = int((totals or {}).get("requests") or 0)
    errors = int((totals or {}).get("errors") or 0)

    def _cost(value: Any) -> float:
        return 0.0 if value is None else float(value)

    def _int(value: Any) -> int:
        return int(value or 0)

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
                "cache_read": _int(row.get("cache_read")),
                "cache_write": _int(row.get("cache_write")),
                "cost_usd": _cost(row["cost_usd"]),
                "errors": agent_errors,
                "error_rate": round(agent_errors / agent_requests, 4) if agent_requests else 0.0,
            }
        )

    return {
        "from": _iso_z(from_ts),
        "to": _iso_z(to_ts),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "requests": requests,
            "errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0.0,
            "tokens_in": _int((totals or {}).get("tokens_in")),
            "tokens_out": _int((totals or {}).get("tokens_out")),
            "cache_read": _int((totals or {}).get("cache_read")),
            "cache_write": _int((totals or {}).get("cache_write")),
            "cache_hit_pct": None
            if (totals or {}).get("cache_hit_pct") is None
            else float((totals or {})["cache_hit_pct"]),
            "cost_usd": _cost((totals or {}).get("cost_usd")),
        },
        "daily_agents": [
            {
                "day": str(row["day"]),
                "agent_hash": row["agent_hash"],
                "requests": int(row["requests"]),
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "errors": int(row["errors"]),
            }
            for row in daily_agents
        ],
        "top_models": [
            {
                "provider": row["provider"],
                "model": row["model"],
                "requests": int(row["requests"]),
                "tokens_in": int(row["tokens_in"]),
                "tokens_out": int(row["tokens_out"]),
                "cache_read": _int(row.get("cache_read")),
                "cache_hit_pct": None if row.get("cache_hit_pct") is None else float(row["cache_hit_pct"]),
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
        "status_codes": [{"status": int(row["status"]), "requests": int(row["requests"])} for row in status_codes],
        "latency_models": [
            {
                "provider": row["provider"],
                "model": row["model"],
                "requests": int(row["requests"]),
                "p50_ms": None if row["p50_ms"] is None else float(row["p50_ms"]),
                "p95_ms": None if row["p95_ms"] is None else float(row["p95_ms"]),
            }
            for row in latency_models
        ],
    }
