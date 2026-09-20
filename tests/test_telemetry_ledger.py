"""Ledger unit tests — fake connections only, no real Postgres (SPEC §6).

TelemetryLedger must be fail-open: every public entry point swallows its own
errors (D4), writes are fire-and-forget through an internal queue drained by
one background task, and reads shape dict_row output into JSON-safe payloads
while letting failures propagate to the HTTP handler.

The read tests key canned rows on the *exact* SQL constants: the SQL strings
are part of this module's contract (the fakes are the only thing standing in
for a database in CI), so a query that changes shape must change here too.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from hivemind.telemetry.ledger import (
    _COLUMN_ORDER,
    _SCHEMA_DDL,
    _insert_params,
    _PRUNE_INTERVAL_S,
    _QUEUE_MAX,
    _RANGE,
    _REQUEST_COLUMNS,
    _SQL_AGENTS,
    _SQL_DAILY_AGENTS,
    _SQL_FACET_AGENTS,
    _SQL_FACET_MODELS,
    _SQL_FACET_PROVIDERS,
    _SQL_LATENCY,
    _SQL_LATENCY_MODELS,
    _SQL_PRUNE,
    _SQL_REQUESTS,
    _SQL_REQUESTS_COUNT,
    _SQL_REQUESTS_EXPORT,
    _SQL_SERIES_DAY,
    _SQL_SERIES_HOUR,
    _SQL_STATUS,
    _SQL_TOP_MODELS,
    _SQL_TOTALS,
    NullLedger,
    TelemetryLedger,
    get_ledger,
    set_ledger,
)

_INSERT_MARK = "INSERT INTO mesh_telemetry.token_usage"

# A fixed display range + the half-open window the ledger must derive from it
# (inclusive `to` -> `to + 1 day`).
FROM_TS = datetime(2026, 9, 1, tzinfo=timezone.utc)
TO_TS = datetime(2026, 9, 14, tzinfo=timezone.utc)
WINDOW = (FROM_TS, TO_TS + timedelta(days=1))

_UNAVAILABLE = {"error": "telemetry unavailable"}


class FakeCursor:
    def __init__(self, conn, sql: str) -> None:
        self._conn = conn
        self._sql = sql

    async def fetchall(self):
        rows = self._conn.fetchall_rows.get(self._sql)
        if rows is None:
            rows = self._conn.fetchall_default
        return list(rows)

    async def fetchone(self):
        row = self._conn.fetchone_rows.get(self._sql)
        if row is None:
            row = self._conn.fetchone_row
        return row


class FakeServerCursor:
    """Named (server-side) cursor stand-in.

    Real named cursors are created by a *sync* ``conn.cursor(name=…)`` and
    then driven with awaits — psycopg3's AsyncConnection.cursor() is a plain
    method, so the fake mirrors that (an awaitable here would let a broken
    implementation pass).
    """

    def __init__(self, conn, name: str, kwargs: dict) -> None:
        self._conn = conn
        self.name = name
        self.kwargs = kwargs
        self.executed: list[tuple[str, tuple | None]] = []
        self.sizes: list[int] = []
        self.closed = False
        self._batches: list[list[dict]] = []

    async def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        self._batches = list(self._conn.export_batches)

    async def fetchmany(self, size: int):
        self.sizes.append(size)
        if self._conn.export_error is not None:
            raise self._conn.export_error
        return self._batches.pop(0) if self._batches else []

    async def close(self) -> None:
        self.closed = True


class FakeConn:
    """Records every execute(); returns canned rows keyed by exact SQL."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.autocommit = False
        self.closed = False
        # fetchall: exact-SQL rows, else `fetchall_default` (the dynamic
        # requests SELECT is built at call time, so it has no constant key).
        self.fetchall_rows: dict[str, list[dict]] = {}
        self.fetchall_default: list[dict] = []
        self.fetchone_rows: dict[str, dict] = {}
        self.fetchone_row: dict | None = None
        self.export_batches: list[list[dict]] = []
        self.export_error: Exception | None = None
        self.cursors: list[FakeServerCursor] = []

    async def set_autocommit(self, value) -> None:
        self.autocommit = value

    async def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        return FakeCursor(self, sql)

    def cursor(self, name: str = "", **kwargs) -> FakeServerCursor:
        cursor = FakeServerCursor(self, name, kwargs)
        self.cursors.append(cursor)
        return cursor

    async def close(self) -> None:
        self.closed = True


def _fake_factory(conn: FakeConn):
    async def factory(dsn: str) -> FakeConn:
        return conn

    return factory


class FakePool:
    """Factory that mints a fresh FakeConn per call and records it.

    The real ledger's pruner opens its own connection, so tests that count
    statements on the reader connection need the pruner's work to land
    somewhere else.
    """

    def __init__(self) -> None:
        self.created: list[FakeConn] = []

    async def __call__(self, dsn: str) -> FakeConn:
        conn = FakeConn()
        self.created.append(conn)
        return conn

    def prunes(self) -> int:
        return sum(1 for conn in self.created for sql, _ in conn.executed if sql == _SQL_PRUNE)


def _inserts(conn: FakeConn) -> list[tuple[str, tuple | None]]:
    return [(sql, params) for sql, params in conn.executed if _INSERT_MARK in sql]


def _reads(conn: FakeConn) -> list[tuple[str, tuple | None]]:
    """Parameterized statements, i.e. everything except the schema DDL."""
    return [(sql, params) for sql, params in conn.executed if params is not None]


def _row(**overrides) -> dict:
    row = {
        "id": 7,
        "ts": datetime(2026, 9, 2, 3, 4, 5, tzinfo=timezone.utc),
        "agent_hash": "bucket-1",
        "provider": "Anthropic",
        "model": "deepseek-chat",
        "tokens_in": 100,
        "tokens_out": 50,
        "cache_read": 40,
        "cache_write": 5,
        "latency_ms": 123.4,
        "status": 200,
    }
    row.update(overrides)
    return row


# --- NullLedger / module singleton -------------------------------------------


async def test_null_ledger_noops():
    ledger = NullLedger()
    await ledger.record({"anything": 1})
    await ledger.connect()
    await ledger.shutdown()
    await ledger.prune()
    assert (await ledger.fetch_dashboard()) == {"error": "telemetry unavailable"}
    assert (await ledger.fetch_dashboard(days=30)) == {"error": "telemetry unavailable"}
    # Every Phase 2 reader answers the same marker so an unconfigured
    # deployment renders "telemetry unavailable" instead of a 500.
    assert (await ledger.fetch_overview(FROM_TS, TO_TS)) == _UNAVAILABLE
    assert (await ledger.fetch_requests(FROM_TS, TO_TS, limit=10)) == _UNAVAILABLE
    assert (await ledger.fetch_facets(FROM_TS, TO_TS)) == _UNAVAILABLE
    assert (await ledger.fetch_series(FROM_TS, TO_TS, "hour")) == _UNAVAILABLE
    # The export path keeps its types: it is iterated, not rendered.
    assert (await ledger.count_requests(FROM_TS, TO_TS)) == 0
    assert [batch async for batch in ledger.export_rows(FROM_TS, TO_TS)] == []


async def test_get_ledger_defaults_to_null(monkeypatch):
    # Fresh import state: unset DSN must mean a no-op ledger (SPEC §6).
    monkeypatch.setattr("hivemind.telemetry.ledger._ledger", NullLedger())
    ledger = get_ledger()
    await ledger.record({"a": 1})
    assert (await ledger.fetch_dashboard()) == {"error": "telemetry unavailable"}


async def test_set_and_get_singleton():
    class Dummy:
        pass

    dummy = Dummy()
    set_ledger(dummy)  # type: ignore[arg-type]
    try:
        assert get_ledger() is dummy
    finally:
        set_ledger(NullLedger())


# --- row formatting ----------------------------------------------------------


def test_insert_params_column_order_and_missing_keys():
    row = {
        "agent_hash": "a",
        "provider": "p",
        "model": "m",
        "tokens_in": 1,
        "tokens_out": 2,
        "cache_read": 3,
        "cache_write": 4,
        "latency_ms": 1.5,
        "status": 200,
        # 'reasoning' missing on purpose -> NULL
    }
    params = _insert_params(row)
    assert len(params) == len(_COLUMN_ORDER)
    for key, value in zip(_COLUMN_ORDER, params, strict=True):
        assert value == row.get(key)
    assert params[_COLUMN_ORDER.index("reasoning")] is None


def test_insert_params_ignores_extra_keys():
    params = _insert_params({"agent_hash": "a", "status": 200, "extra": "ignored"})
    assert len(params) == len(_COLUMN_ORDER)
    assert params[_COLUMN_ORDER.index("agent_hash")] == "a"
    assert params[_COLUMN_ORDER.index("status")] == 200
    assert params[_COLUMN_ORDER.index("tokens_in")] is None


# --- connect / schema --------------------------------------------------------


async def test_connect_runs_schema_ddl():
    conn = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(conn))
    await ledger.connect()
    statements = [sql for sql, _ in conn.executed]
    assert len(statements) == 7
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS mesh_telemetry"
    assert "CREATE TABLE IF NOT EXISTS mesh_telemetry.token_usage" in statements[1]
    assert "usage_cost" in statements[-1]
    assert conn.autocommit is True
    assert not conn.closed  # writer connection stays open for the next row
    await ledger.shutdown()  # stops the pruner connect() just started


async def test_connect_failure_is_swallowed():
    async def boom(dsn):
        raise ConnectionError("pg down")

    ledger = TelemetryLedger("postgresql://fake", conn_factory=boom)
    await ledger.connect()  # must not raise (D4)
    assert ledger._pruner is None  # nothing to prune without a connection


# --- write path --------------------------------------------------------------


async def test_record_inserts_one_row_in_column_order():
    conn = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(conn))
    await ledger.connect()
    row = {
        "agent_hash": "bucket-1",
        "provider": "Anthropic",
        "model": "deepseek-chat",
        "tokens_in": 100,
        "tokens_out": 50,
        "cache_read": 40,
        "cache_write": 5,
        "reasoning": None,
        "latency_ms": 123.4,
        "status": 200,
    }
    await ledger.record(row)
    await ledger.shutdown()  # drains the queue, then closes

    inserts = _inserts(conn)
    assert len(inserts) == 1
    assert inserts[0][1] == tuple(row.get(col) for col in _COLUMN_ORDER)
    assert conn.closed is True


async def test_record_with_missing_optional_fields_uses_null():
    conn = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(conn))
    await ledger.connect()
    await ledger.record({"agent_hash": "a", "provider": "p", "model": "m", "status": 200})
    await ledger.shutdown()

    inserts = _inserts(conn)
    assert len(inserts) == 1
    assert inserts[0][1][_COLUMN_ORDER.index("tokens_in")] is None
    assert inserts[0][1][_COLUMN_ORDER.index("cache_read")] is None


async def test_write_failure_is_swallowed_and_reconnects_lazily(caplog):
    """A dead Postgres drops rows at DEBUG and never raises (D4)."""
    state = {"calls": 0, "up": False}
    ok_conn = FakeConn()

    async def flaky_factory(dsn):
        state["calls"] += 1
        if not state["up"]:
            raise ConnectionError("pg down")
        return ok_conn

    ledger = TelemetryLedger("postgresql://fake", conn_factory=flaky_factory)
    with caplog.at_level(logging.DEBUG, logger="hivemind.telemetry.ledger"):
        await ledger.connect()  # DB down at startup -> swallowed
        await ledger.record({"agent_hash": "a", "provider": "p", "model": "m", "status": 200})
        for _ in range(20):  # let the worker attempt the failing write
            await asyncio.sleep(0)

    assert not ledger._conn  # failed connection was dropped, not kept
    assert not _inserts(ok_conn)  # the dead-PG row never reached Postgres
    messages = [r.getMessage() for r in caplog.records]
    assert any("connect failed" in m for m in messages)
    assert any("write failed" in m for m in messages)

    # Postgres comes back: the next write reconnects lazily and lands.
    state["up"] = True
    await ledger.record({"agent_hash": "b", "provider": "p", "model": "m", "status": 200})
    await ledger.shutdown()
    inserts = _inserts(ok_conn)
    assert len(inserts) == 1
    assert inserts[0][1][_COLUMN_ORDER.index("agent_hash")] == "b"
    assert ok_conn.closed is True


async def test_record_never_raises_when_queue_full():
    conn = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(conn))
    await ledger.connect()

    # Back the worker out (it never consumes) so the bounded queue fills.
    release = asyncio.Event()

    async def stuck_worker():
        await release.wait()

    ledger._queue = asyncio.Queue(maxsize=_QUEUE_MAX)  # type: ignore[assignment]
    ledger._worker = asyncio.create_task(stuck_worker())  # type: ignore[assignment]
    for _ in range(_QUEUE_MAX + 1_000):
        await ledger.record({"agent_hash": "a", "provider": "p", "model": "m", "status": 200})
    assert ledger._queue.qsize() == _QUEUE_MAX  # excess dropped, nothing raised

    release.set()  # shutdown's sentinel put may hit the full queue — fine
    await ledger.shutdown()


async def test_shutdown_is_idempotent_and_drains():
    conn = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(conn))
    await ledger.connect()
    for i in range(5):
        await ledger.record({"agent_hash": f"a{i}", "provider": "p", "model": "m", "status": 200})
    await ledger.shutdown()
    await ledger.shutdown()  # idempotent
    assert len(_inserts(conn)) == 5
    # A closed ledger drops further records silently (never raises).
    await ledger.record({"agent_hash": "x", "provider": "p", "model": "m", "status": 200})
    assert len(_inserts(conn)) == 5


# --- read path: overview -----------------------------------------------------


def _overview_fixtures(holder: FakeConn) -> None:
    holder.fetchall_rows = {
        _SQL_DAILY_AGENTS: [
            {
                "day": date(2026, 9, 1),
                "agent_hash": "fp-abc123",
                "requests": 10,
                "tokens_in": 1000,
                "tokens_out": 500,
                "errors": 1,
            },
            {
                "day": date(2026, 9, 1),
                "agent_hash": "Other",
                "requests": 4,
                "tokens_in": 400,
                "tokens_out": 300,
                "errors": 0,
            },
        ],
        _SQL_TOP_MODELS: [
            {
                "provider": "Anthropic",
                "model": "deepseek-chat",
                "requests": 9,
                "tokens_in": 900,
                "tokens_out": 400,
                "cache_read": 300,
                "cost_usd": 0.0111,
            },
            {
                "provider": "Ollama (local)",
                "model": "llama3.2:1b",
                "requests": 4,
                "tokens_in": 400,
                "tokens_out": 300,
                "cache_read": 0,
                "cost_usd": 0.0,
            },
        ],
        _SQL_AGENTS: [
            {
                "agent_hash": "bucket-1",
                "requests": 25,
                "tokens_in": 2500,
                "tokens_out": 1000,
                "cache_read": 1500,
                "cache_write": 40,
                "cost_usd": 0.03,
                "errors": 3,
            }
        ],
        _SQL_LATENCY: [
            {
                "provider": "Anthropic",
                "requests": 9,
                "p50_ms": 1234.5,
                "p95_ms": 8901.2,
            },
            {
                "provider": "Ollama (local)",
                "requests": 4,
                "p50_ms": None,
                "p95_ms": None,
            },
        ],
        _SQL_STATUS: [
            {"status": 200, "requests": 11},
            {"status": 500, "requests": 3},
        ],
        _SQL_LATENCY_MODELS: [
            {
                "provider": "Anthropic",
                "model": "deepseek-chat",
                "requests": 9,
                "p50_ms": 1200.0,
                "p95_ms": 8100.0,
            }
        ],
    }
    holder.fetchone_rows = {
        _SQL_TOTALS: {
            "requests": 14,
            "errors": 3,
            "tokens_in": 1400,
            "tokens_out": 800,
            "cache_read": 600,
            "cache_write": 25,
            "cache_hit_pct": 0.3,
            "cost_usd": 0.0123,
        }
    }


async def test_fetch_overview_shapes_payload_and_windows_the_reads():
    holder = FakeConn()
    _overview_fixtures(holder)
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    payload = await ledger.fetch_overview(FROM_TS, TO_TS)

    # The display range replaces the old `days` counter in the payload.
    assert "days" not in payload
    assert payload["from"] == "2026-09-01T00:00:00Z"
    assert payload["to"] == "2026-09-14T00:00:00Z"
    assert payload["generated_at"].endswith("+00:00")
    totals = payload["totals"]
    assert totals == {
        "requests": 14,
        "errors": 3,
        "error_rate": round(3 / 14, 4),
        "tokens_in": 1400,
        "tokens_out": 800,
        "cache_read": 600,
        "cache_write": 25,
        # 600 / (600 + 1400) — real input is cache_read + fresh-only tokens_in.
        "cache_hit_pct": 0.3,
        "cost_usd": 0.0123,
    }
    assert payload["daily_agents"][0] == {
        "day": "2026-09-01",
        "agent_hash": "fp-abc123",
        "requests": 10,
        "tokens_in": 1000,
        "tokens_out": 500,
        "errors": 1,
    }
    assert payload["daily_agents"][1]["agent_hash"] == "Other"
    assert payload["top_models"][0]["model"] == "deepseek-chat"
    assert payload["top_models"][0]["cache_read"] == 300
    assert payload["agents"] == [
        {
            "agent_hash": "bucket-1",
            "requests": 25,
            "tokens_in": 2500,
            "tokens_out": 1000,
            "cache_read": 1500,
            "cache_write": 40,
            "cost_usd": 0.03,
            "errors": 3,
            "error_rate": 0.12,
        }
    ]
    assert payload["latency"][0]["p50_ms"] == 1234.5
    assert payload["latency"][1]["p50_ms"] is None
    assert payload["status_codes"] == [
        {"status": 200, "requests": 11},
        {"status": 500, "requests": 3},
    ]
    assert payload["latency_models"] == [
        {
            "provider": "Anthropic",
            "model": "deepseek-chat",
            "requests": 9,
            "p50_ms": 1200.0,
            "p95_ms": 8100.0,
        }
    ]
    # Everything must be JSON-serializable (no Decimal/date leakage).
    json.dumps(payload)

    # Seven queries, every one on the same half-open window: the display `to`
    # is a date the user sees, the SQL bound is exclusive (D7).
    reads = _reads(holder)
    assert len(reads) == 7
    assert all(params == WINDOW for _, params in reads)
    # Schema DDL also ran on the reader connection (self-healing view).  The
    # multi-line statements begin with a newline, so match on content.
    ddl = [sql for sql, _ in holder.executed if "CREATE" in sql]
    assert len(ddl) == 5
    assert holder.closed is True, "short-lived reader connection must be closed"


async def test_fetch_overview_with_no_rows():
    holder = FakeConn()
    holder.fetchone_row = {
        "requests": 0,
        "errors": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
    }
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))
    payload = await ledger.fetch_overview(FROM_TS, TO_TS)
    assert payload["totals"] == {
        "requests": 0,
        "errors": 0,
        "error_rate": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cache_hit_pct": None,  # 0 / 0 — no ratio to report
        "cost_usd": 0.0,
    }
    assert payload["daily_agents"] == []
    assert payload["top_models"] == []
    assert payload["agents"] == []
    assert payload["latency"] == []
    assert payload["status_codes"] == []
    assert payload["latency_models"] == []
    json.dumps(payload)


async def test_fetch_overview_reader_failure_raises_for_handler():
    """The reader may raise; the HTTP handler owns mapping it to the
    'telemetry unavailable' payload (a real DB failure raises; only
    NullLedger returns the error payload directly)."""

    async def boom(dsn):
        raise ConnectionError("pg down")

    ledger = TelemetryLedger("postgresql://fake", conn_factory=boom)
    with pytest.raises(ConnectionError):
        await ledger.fetch_overview(FROM_TS, TO_TS)


async def test_fetch_dashboard_alias_maps_days_onto_a_range():
    """Phase 1's `days=N` still works, as `[now - N days, now]`."""
    holder = FakeConn()
    _overview_fixtures(holder)
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    payload = await ledger.fetch_dashboard(days=7)

    reads = _reads(holder)
    assert len(reads) == 7
    window_from, window_to = reads[0][1]
    # days=7 covers today plus the six before it, so the half-open window is
    # eight days wide (the +1 day on the exclusive bound).
    assert window_to - window_from == timedelta(days=8)
    assert abs((datetime.now(timezone.utc) - window_from) - timedelta(days=7)) < timedelta(minutes=1)
    assert payload["to"].endswith("Z")


# --- read path: requests -----------------------------------------------------


async def test_fetch_requests_placeholder_order_contract():
    holder = FakeConn()
    holder.fetchone_rows = {
        _SQL_REQUESTS_COUNT.format(where=_RANGE + " AND agent_hash = %s AND status = %s"): {"total": 42}
    }
    holder.fetchall_default = [_row()]
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    payload = await ledger.fetch_requests(
        FROM_TS,
        TO_TS,
        agent_hash="bucket-1",
        status=200,
        sort="tokens",
        order="asc",
        limit=50,
        offset=100,
    )

    reads = _reads(holder)
    count_sql, count_params = reads[0]
    rows_sql, rows_params = reads[1]
    where = _RANGE + " AND agent_hash = %s AND status = %s"
    assert count_sql == _SQL_REQUESTS_COUNT.format(where=where)
    # (from, to), filters in _REQUEST_FILTERS order, then limit, offset.
    assert count_params == (FROM_TS, TO_TS + timedelta(days=1), "bucket-1", 200)
    assert rows_sql == _SQL_REQUESTS.format(
        columns=", ".join(_REQUEST_COLUMNS),
        where=where,
        order_by="(coalesce(tokens_in, 0) + coalesce(tokens_out, 0)) ASC NULLS LAST, id ASC",
    )
    assert rows_params == (FROM_TS, TO_TS + timedelta(days=1), "bucket-1", 200, 50, 100)
    assert "LIMIT %s OFFSET %s" in rows_sql

    assert payload["total"] == 42
    assert payload["limit"] == 50
    assert payload["offset"] == 100
    assert payload["from"] == "2026-09-01T00:00:00Z"
    assert payload["to"] == "2026-09-14T00:00:00Z"
    assert payload["rows"] == [_row(id=7, ts="2026-09-02T03:04:05Z")]
    json.dumps(payload)
    assert holder.closed is True


async def test_fetch_requests_defaults_and_nullable_columns():
    holder = FakeConn()
    holder.fetchone_rows = {_SQL_REQUESTS_COUNT.format(where=_RANGE): {"total": 1}}
    holder.fetchall_default = [_row(tokens_in=None, cache_read=None, cache_write=None, latency_ms=None)]
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    payload = await ledger.fetch_requests(FROM_TS, TO_TS)

    count_sql, count_params = _reads(holder)[0]
    rows_sql, rows_params = _reads(holder)[1]
    assert count_sql == _SQL_REQUESTS_COUNT.format(where=_RANGE)
    assert count_params == WINDOW
    assert "ORDER BY ts DESC NULLS LAST, id DESC" in rows_sql
    assert rows_params == WINDOW + (100, 0)  # default page size, first page
    # "Not reported" must stay distinguishable from zero.
    assert payload["rows"][0]["tokens_in"] is None
    assert payload["rows"][0]["latency_ms"] is None


async def test_fetch_requests_unwhitelisted_sort_and_order_fall_back():
    holder = FakeConn()
    holder.fetchone_row = {"total": 0}
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    await ledger.fetch_requests(FROM_TS, TO_TS, sort="ts; DROP TABLE token_usage", order="desc; --")

    sql = _reads(holder)[1][0]
    assert "DROP" not in sql
    assert "--" not in sql
    assert "ORDER BY ts DESC NULLS LAST, id DESC" in sql


async def test_fetch_requests_refuses_negative_paging():
    """The upper clamps are the handler's job, but a negative LIMIT is a SQL
    error rather than a clamp, so the ledger floors it before it gets there."""
    holder = FakeConn()
    holder.fetchone_row = {"total": 0}
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    payload = await ledger.fetch_requests(FROM_TS, TO_TS, limit=-5, offset=-100)

    assert _reads(holder)[1][1][-2:] == (1, 0)
    assert payload["limit"] == 1
    assert payload["offset"] == 0


# --- read path: facets -------------------------------------------------------


async def test_fetch_facets_shapes_all_three_lists():
    holder = FakeConn()
    holder.fetchall_rows = {
        _SQL_FACET_AGENTS: [{"agent_hash": "bucket-1", "requests": 9}],
        _SQL_FACET_MODELS: [{"provider": "Anthropic", "model": "deepseek-chat", "requests": 8}],
        _SQL_FACET_PROVIDERS: [
            {"provider": "Anthropic", "requests": 8},
            {"provider": "Ollama (local)", "requests": 1},
        ],
    }
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    payload = await ledger.fetch_facets(FROM_TS, TO_TS)

    assert payload == {
        "agents": [{"agent_hash": "bucket-1", "requests": 9}],
        "models": [{"provider": "Anthropic", "model": "deepseek-chat", "requests": 8}],
        "providers": [
            {"provider": "Anthropic", "requests": 8},
            {"provider": "Ollama (local)", "requests": 1},
        ],
    }
    assert [_reads(holder)[i][1] for i in range(3)] == [WINDOW, WINDOW, WINDOW]


# --- read path: series -------------------------------------------------------


async def test_fetch_series_uses_the_whitelisted_bucket_queries():
    holder = FakeConn()
    holder.fetchall_rows = {
        _SQL_SERIES_HOUR.format(where=_RANGE): [
            {
                "bucket_start": datetime(2026, 9, 2, 3),  # naive: UTC by contract
                "requests": 5,
                "tokens_in": 500,
                "tokens_out": 250,
                "cache_read": 100,
                "errors": 1,
            }
        ],
        _SQL_SERIES_DAY.format(where=_RANGE): [],
    }
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    hourly = await ledger.fetch_series(FROM_TS, TO_TS, "hour")
    daily = await ledger.fetch_series(FROM_TS, TO_TS, "day")

    assert hourly == {
        "bucket": "hour",
        "rows": [
            {
                "bucket_start": "2026-09-02T03:00:00Z",
                "requests": 5,
                "tokens_in": 500,
                "tokens_out": 250,
                "cache_read": 100,
                "errors": 1,
            }
        ],
    }
    assert daily == {"bucket": "day", "rows": []}
    assert _reads(holder)[0][0] == _SQL_SERIES_HOUR.format(where=_RANGE)
    assert _reads(holder)[1][0] == _SQL_SERIES_DAY.format(where=_RANGE)


async def test_fetch_series_rejects_unknown_bucket():
    holder = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    with pytest.raises(ValueError):
        await ledger.fetch_series(FROM_TS, TO_TS, "minute; DROP TABLE token_usage")

    assert holder.executed == []  # rejected before any connection was opened


async def test_fetch_series_appends_filters_in_whitelist_order():
    holder = FakeConn()
    holder.fetchall_rows = {_SQL_SERIES_HOUR.format(where=_RANGE + " AND agent_hash = %s AND provider = %s"): []}
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    await ledger.fetch_series(FROM_TS, TO_TS, "hour", agent_hash="bucket-1", provider="Anthropic")

    sql, params = _reads(holder)[0]
    assert sql == _SQL_SERIES_HOUR.format(where=_RANGE + " AND agent_hash = %s AND provider = %s")
    assert params == WINDOW + ("bucket-1", "Anthropic")


# --- read path: count + export ----------------------------------------------


async def test_count_requests_returns_the_count():
    holder = FakeConn()
    holder.fetchone_rows = {_SQL_REQUESTS_COUNT.format(where=_RANGE + " AND model = %s"): {"total": 1234}}
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    total = await ledger.count_requests(FROM_TS, TO_TS, filters={"model": "deepseek-chat", "limit": 10})

    assert total == 1234
    assert _reads(holder) == [
        (_SQL_REQUESTS_COUNT.format(where=_RANGE + " AND model = %s"), WINDOW + ("deepseek-chat",))
    ]
    assert holder.closed is True


async def test_export_rows_streams_named_cursor_batches():
    holder = FakeConn()
    holder.export_batches = [[_row(id=3), _row(id=2)], [_row(id=1)]]
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    batches = [batch async for batch in ledger.export_rows(FROM_TS, TO_TS, batch_size=2)]

    assert [len(batch) for batch in batches] == [2, 1]
    assert batches[0][0]["ts"] == "2026-09-02T03:04:05Z"  # shaped, JSON-safe
    assert json.dumps(batches[0][0])
    assert len(holder.cursors) == 1
    cursor = holder.cursors[0]
    assert cursor.name == "telemetry_export"
    assert cursor.kwargs == {"withhold": True}  # autocommit needs WITH HOLD
    assert cursor.sizes == [2, 2, 2]  # fetched until the empty batch ended it
    sql, params = cursor.executed[0]
    assert sql == _SQL_REQUESTS_EXPORT.format(
        columns=", ".join(_REQUEST_COLUMNS),
        where=_RANGE,
        order_by="ts DESC NULLS LAST, id DESC",
    )
    assert params == WINDOW
    assert cursor.closed is True
    assert holder.closed is True


async def test_export_rows_with_filters_and_empty_result():
    holder = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    batches = [
        batch
        async for batch in ledger.export_rows(
            FROM_TS, TO_TS, filters={"agent_hash": "bucket-1", "status": 500, "sort": "ts"}
        )
    ]

    assert batches == []
    cursor = holder.cursors[0]
    sql, params = cursor.executed[0]
    assert sql.endswith(_RANGE + " AND agent_hash = %s AND status = %s\nORDER BY ts DESC NULLS LAST, id DESC\n")
    assert params == WINDOW + ("bucket-1", 500)
    assert cursor.closed is True


async def test_export_rows_closes_cursor_and_connection_on_failure():
    holder = FakeConn()
    holder.export_batches = [[_row()]]
    holder.export_error = RuntimeError("pg vanished mid-stream")
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))

    with pytest.raises(RuntimeError):
        async for _ in ledger.export_rows(FROM_TS, TO_TS):
            pass

    assert holder.cursors[0].closed is True
    assert holder.closed is True


# --- retention (prune) -------------------------------------------------------


async def test_prune_deletes_with_the_retention_window():
    conn = FakeConn()
    ledger = TelemetryLedger("postgresql://fake", retention_days=30, conn_factory=_fake_factory(conn))

    await ledger.prune()

    assert conn.executed == [(_SQL_PRUNE, (30,))]
    assert conn.autocommit is True
    assert conn.closed is True  # short-lived: never the writer's connection
    assert ledger.retention_days == 30


def test_retention_defaults_and_floor():
    assert TelemetryLedger("postgresql://fake").retention_days == 90
    # A zero/negative retention would delete everything; the ledger floors it.
    assert TelemetryLedger("postgresql://fake", retention_days=0).retention_days == 1


async def test_prune_swallows_failures(caplog):
    async def boom(dsn):
        raise ConnectionError("pg down")

    ledger = TelemetryLedger("postgresql://fake", conn_factory=boom)
    with caplog.at_level(logging.DEBUG, logger="hivemind.telemetry.ledger"):
        await ledger.prune()  # must not raise (D10)

    assert any("prune failed" in r.getMessage() for r in caplog.records)


async def test_pruner_retries_after_a_failed_startup_connect(monkeypatch):
    """If the ledger's FIRST connect fails, retention must not stay silently
    disabled for the process lifetime: the pruner re-arms on the next
    successful connection (here, a write's lazy reconnect) and a DELETE
    eventually runs (D10, fail-open throughout)."""
    monkeypatch.setattr("hivemind.telemetry.ledger._PRUNE_INTERVAL_S", 0.01)
    state = {"up": False}
    pool = FakePool()

    async def flaky(dsn: str):
        if not state["up"]:
            raise ConnectionError("pg down")
        return await pool(dsn)

    ledger = TelemetryLedger("postgresql://fake", conn_factory=flaky)
    await ledger.connect()  # swallowed (D4)
    assert ledger._pruner is None  # nothing armed without a connection

    state["up"] = True  # Postgres comes back
    await ledger.record({"agent_hash": "a", "provider": "p", "model": "m", "status": 200})
    for _ in range(200):
        if pool.prunes() >= 1:
            break
        await asyncio.sleep(0.01)

    assert pool.prunes() >= 1  # a prune statement ran on a later connection
    assert ledger._pruner is not None
    await ledger.shutdown()


async def test_pruner_runs_on_connect_then_stops_on_shutdown(monkeypatch):
    monkeypatch.setattr("hivemind.telemetry.ledger._PRUNE_INTERVAL_S", 0.01)
    pool = FakePool()
    ledger = TelemetryLedger("postgresql://fake", conn_factory=pool)

    await ledger.connect()
    for _ in range(200):  # the first pass is immediate; the loop then repeats
        if pool.prunes() >= 2:
            break
        await asyncio.sleep(0.01)
    assert pool.prunes() >= 2
    assert ledger._pruner is not None

    await ledger.shutdown()

    assert ledger._pruner is None
    assert ledger._closed is True
    prunes_at_shutdown = pool.prunes()
    for _ in range(5):
        await asyncio.sleep(0.01)
    assert pool.prunes() == prunes_at_shutdown  # cancelled: no further deletes


async def test_prune_interval_is_a_day_by_default():
    assert _PRUNE_INTERVAL_S == 24 * 60 * 60


# --- real Postgres (MESH_TEST_DSN) -------------------------------------------

_PG_TAG = "ledger-pgtest"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MESH_TEST_DSN"),
    reason="set MESH_TEST_DSN to run against a real Postgres",
)
async def test_schema_ddl_executes_against_real_postgres():
    """Regression: the view DDL used round(double precision, integer) which
    does not exist in Postgres — every connect failed at ensure_schema and
    the fail-open swallow silently dropped all telemetry (2026-09-03).
    With MESH_TEST_DSN set this proves the full DDL (tables + view) parses,
    that a row round-trips through the Phase 2 readers (requests page +
    streamed export over a real named cursor), and that pruning deletes by
    age only."""
    import psycopg

    dsn = os.environ["MESH_TEST_DSN"]
    fresh_tag, old_tag = f"{_PG_TAG}-fresh", f"{_PG_TAG}-old"
    now = datetime.now(timezone.utc)

    async def _cleanup() -> None:
        conn = await psycopg.AsyncConnection.connect(dsn)
        try:
            await conn.set_autocommit(True)
            for tag in (fresh_tag, old_tag):
                await conn.execute("DELETE FROM mesh_telemetry.token_usage WHERE agent_hash = %s", (tag,))
        finally:
            await conn.close()

    await _cleanup()  # a previous failed run must not pollute the assertions
    ledger = TelemetryLedger(dsn)
    await ledger.connect()
    try:
        await ledger.record(
            {
                "agent_hash": fresh_tag,
                "provider": "Anthropic",
                "model": "ddl-model",
                "tokens_in": 100,
                "tokens_out": 50,
                "latency_ms": 2.0,
                "status": 200,
            }
        )
        await ledger.record(
            {
                "agent_hash": old_tag,
                "provider": "Anthropic",
                "model": "ddl-model",
                "tokens_in": 10,
                "tokens_out": 5,
                "status": 200,
            }
        )
        await asyncio.sleep(1.5)  # let the write queue drain

        payload = await ledger.fetch_overview(now - timedelta(days=1), now)
        assert isinstance(payload, dict) and "error" not in payload  # live view queried

        page = await ledger.fetch_requests(now - timedelta(days=1), now, agent_hash=fresh_tag)
        assert page["total"] == 1
        assert page["rows"][0]["agent_hash"] == fresh_tag
        assert page["rows"][0]["ts"].endswith("Z")

        # Streamed export over a real WITH HOLD cursor: one shaped row, then
        # an empty batch (the cursor loop must terminate on it).
        batches = [
            batch async for batch in ledger.export_rows(now - timedelta(days=1), now, filters={"agent_hash": fresh_tag})
        ]
        assert [row["agent_hash"] for batch in batches for row in batch] == [fresh_tag]

        # Prune by age: backdate the old row past the retention, then the
        # data that is too old goes and the fresh row stays.
        conn = await psycopg.AsyncConnection.connect(dsn)
        try:
            await conn.set_autocommit(True)
            await conn.execute(
                "UPDATE mesh_telemetry.token_usage SET ts = now() - interval '120 days' WHERE agent_hash = %s",
                (old_tag,),
            )
        finally:
            await conn.close()

        await TelemetryLedger(dsn, retention_days=90).prune()

        assert await ledger.count_requests(now - timedelta(days=200), now, filters={"agent_hash": old_tag}) == 0
        assert await ledger.count_requests(now - timedelta(days=200), now, filters={"agent_hash": fresh_tag}) == 1
    finally:
        await ledger.shutdown()
        await _cleanup()


# --- DDL parse smoke test (regression: the trailing-comma incident) ---------


def test_schema_ddl_parses_against_real_postgres():
    """Every _SCHEMA_DDL statement must PARSE on real PostgreSQL.

    The fake-connection tests above only record statement strings — a SQL
    syntax error ships invisible to them (the 2026-09-17 outage: one
    trailing comma zeroed all telemetry).  This smoke test runs each
    statement inside a rolled-back transaction against the local Postgres
    and skips (not fails) when that database is unavailable, so CI
    without PG still passes.
    """
    pytest.importorskip("psycopg")
    import psycopg

    dsn = os.environ.get(
        "HIVEMIND_TEST_DB_URL", "postgresql://hivemind@localhost:5432/hivemind_test"
    )
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception:
        pytest.skip("local Postgres unavailable")
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            for statement in _SCHEMA_DDL:
                cur.execute(statement)
        except Exception as exc:
            pytest.fail(f"schema DDL does not parse: {exc}\nstatement: {statement!r}")
        finally:
            cur.execute("ROLLBACK")
    finally:
        conn.close()


# --- Read-SQL guards (regression: the 8450-fix comment broke every read) ----


def test_no_bare_percent_in_any_sql_constant():
    """psycopg scans the WHOLE statement for placeholders, comments
    included: a literal percent that is not %s or %% raises
    'incomplete placeholder' at execute time.  The 8450-tile fix shipped
    exactly that in a SQL comment and would have killed every read on
    restart.

    Collection is by CONTENT, not name: an earlier version filtered
    dir(L) by ``name.startswith("_SQL_")`` and silently missed
    _INSERT_SQL and the _REQUEST_FILTERS fragments — a commit message
    claimed those were covered and the claim was false (the hostile
    verifier planted a bare percent in a filter fragment, nothing caught
    it, and that SQL runs WITH params at runtime).  So this lint covers
    three layers: every module-level string containing a SQL keyword,
    the explicit whitelist of keyword-free fragments/identifiers, and
    the DYNAMIC path — the exact statements the drill-down/export
    readers assemble at call time."""
    import re

    from hivemind.telemetry import ledger as L

    keyword = re.compile(r"\b(?:select|insert|delete|create)\b|do\s*\$\$", re.IGNORECASE)

    def _walk(value, out: list[str]) -> None:
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (tuple, list, frozenset)):
            for item in value:
                _walk(item, out)
        elif isinstance(value, dict):
            for key, item in value.items():
                _walk(key, out)
                _walk(item, out)

    everything: list[str] = []
    for name in dir(L):
        if not name.startswith("__"):  # skip dunders (module docstring et al.)
            _walk(getattr(L, name), everything)
    by_content = [s for s in everything if keyword.search(s)]

    # Fragments and identifiers too small to carry a keyword: the filter
    # clauses, the sort/order whitelists, the range predicate itself.
    explicit: list[str] = []
    for name in dir(L):
        if name.startswith("_SQL_") or name in {
            "_INSERT_SQL",
            "_SCHEMA_DDL",
            "_RANGE",
            "_REQUEST_FILTERS",
            "_REQUEST_SORTS",
            "_REQUEST_ORDERS",
        }:
            _walk(getattr(L, name), explicit)
    explicit = [s for s in explicit if s not in by_content]

    # The dynamic path, assembled exactly the way fetch_requests /
    # count_requests / export_rows / fetch_series build it — all filters
    # enabled, so every _REQUEST_FILTERS clause appears in context.
    from_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    to_dt = datetime(2026, 1, 14, tzinfo=timezone.utc)
    all_filters = {"agent_hash": "b", "model": "m", "provider": "p", "status": 200}
    where_all, _ = L._build_request_where(from_dt, to_dt, all_filters)
    series_where, _ = L._build_request_where(from_dt, to_dt, {k: v for k, v in all_filters.items() if k != "status"})
    columns = ", ".join(L._REQUEST_COLUMNS)
    dynamic = [
        L._SQL_REQUESTS.format(columns=columns, where=where_all, order_by=L._order_by("tokens", "desc")),
        L._SQL_REQUESTS_COUNT.format(where=where_all),
        L._SQL_REQUESTS_EXPORT.format(columns=columns, where=where_all, order_by=L._order_by("status", "asc")),
        L._SQL_SERIES_HOUR.format(where=series_where),
        L._SQL_SERIES_DAY.format(where=series_where),
    ]

    linted = by_content + explicit + dynamic
    # Pins for the false-claim incident: the write statement, a filter
    # fragment and the DDL must all be IN the linted set — the name-based
    # scan missed every one of them.
    assert L._INSERT_SQL in linted
    assert "agent_hash = %s" in linted
    assert any("CREATE TABLE" in s for s in linted)
    assert any("DELETE FROM" in s for s in linted)
    assert len(linted) >= 20, "expected SQL constants + fragments + dynamic variants in the linted set"

    for sql in linted:
        offenders = re.findall(r"%(?!s)(?!%)", sql.replace("%%", ""))
        assert not offenders, f"bare percent in SQL text: {sql[:120]!r}"


def test_every_read_sql_constant_parses_on_real_postgres():
    """The fake-connection tests only record strings; the trailing-comma
    incident added a DDL smoke test, and the 8450-fix comment proved the
    READ queries need the same.  Each constant executes (parameterized,
    inside a rolled-back transaction) against local Postgres; skips when
    unavailable.

    The dynamic drill-down/export statements parse too — the templates
    formatted by ``_build_request_where`` with EVERY filter enabled, the
    exact strings those readers run WITH params (where a planted bare
    percent is a live incident, not a lint nit).  The all-filters WHERE
    bodies are also how each _REQUEST_FILTERS fragment gets exercised:
    a fragment alone is not a statement."""
    pytest.importorskip("psycopg")
    import psycopg

    from hivemind.telemetry import ledger as L
    from hivemind.telemetry.ledger import _SQL_TOTALS

    dsn = os.environ.get(
        "HIVEMIND_TEST_DB_URL", "postgresql://hivemind@localhost:5432/hivemind_test"
    )
    try:
        conn = psycopg.connect(dsn, connect_timeout=3)
    except Exception:
        pytest.skip("local Postgres unavailable")
    window = ("2026-01-01", "2026-01-02")
    columns = ", ".join(L._REQUEST_COLUMNS)
    from_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    to_dt = datetime(2026, 1, 14, tzinfo=timezone.utc)
    all_filters = {"agent_hash": "bucket-1", "model": "m", "provider": "p", "status": 200}
    where_all, params_all = L._build_request_where(from_dt, to_dt, all_filters)
    series_where, series_params = L._build_request_where(
        from_dt, to_dt, {k: v for k, v in all_filters.items() if k != "status"}
    )
    cases: list[tuple[str, str, tuple]] = [
        ("TOTALS", _SQL_TOTALS, window),
        ("TOP_MODELS", L._SQL_TOP_MODELS, window),
        ("AGENTS", L._SQL_AGENTS, window),
        ("DAILY_AGENTS", L._SQL_DAILY_AGENTS, window),
        ("LATENCY", L._SQL_LATENCY, window),
        ("STATUS", L._SQL_STATUS, window),
        ("LATENCY_MODELS", L._SQL_LATENCY_MODELS, window),
        ("SERIES_HOUR", L._SQL_SERIES_HOUR.format(where=L._RANGE), window),
        ("SERIES_DAY", L._SQL_SERIES_DAY.format(where=L._RANGE), window),
        ("FACET_AGENTS", L._SQL_FACET_AGENTS, window),
        ("FACET_MODELS", L._SQL_FACET_MODELS, window),
        ("FACET_PROVIDERS", L._SQL_FACET_PROVIDERS, window),
        # One representative fully-filtered variant per dynamic template.
        (
            "REQUESTS+ALL_FILTERS",
            L._SQL_REQUESTS.format(columns=columns, where=where_all, order_by=L._order_by("status", "asc")),
            params_all + (100, 0),  # trailing LIMIT/OFFSET placeholders
        ),
        ("REQUESTS_COUNT+ALL_FILTERS", L._SQL_REQUESTS_COUNT.format(where=where_all), params_all),
        (
            "REQUESTS_EXPORT+ALL_FILTERS",
            L._SQL_REQUESTS_EXPORT.format(columns=columns, where=where_all, order_by=L._order_by("ts", "desc")),
            params_all,
        ),
        ("SERIES_HOUR+ALL_FILTERS", L._SQL_SERIES_HOUR.format(where=series_where), series_params),
        ("SERIES_DAY+ALL_FILTERS", L._SQL_SERIES_DAY.format(where=series_where), series_params),
    ]
    # The one statement that runs WITH params from the write path — a bare
    # percent here passes every other guard while breaking every real write
    # (proven by the hostile verifier's plant).  Parse check inside the
    # same rolled-back transaction: INSERT ... ROLLBACK writes nothing.
    # Column types per _COLUMN_ORDER: text, text, text, bigint x5,
    # double, int, text — properly typed so the parse check is real.
    insert_params = ("bucket", "prov", "model", 1, 1, 1, 1, None, 1.0, 200, None)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            # CI's hivemind_test is a FRESH database: the read constants
            # reference mesh_telemetry tables that must exist before the
            # parse check.  DDL inside the same rolled-back transaction —
            # schema appears, every statement parses, nothing persists.
            for statement in L._SCHEMA_DDL:
                cur.execute(statement)
            for name, sql, params in cases:
                cur.execute(sql, params)
                cur.fetchall()
            cur.execute(L._INSERT_SQL, insert_params)  # no records to fetch
        except Exception as exc:
            pytest.fail(f"read SQL does not parse ({name}): {exc}")
        finally:
            cur.execute("ROLLBACK")
    finally:
        conn.close()
