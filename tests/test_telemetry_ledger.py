"""Ledger unit tests — fake connections only, no real Postgres (SPEC §6).

TelemetryLedger must be fail-open: every public entry point swallows its own
errors (D4), writes are fire-and-forget through an internal queue drained by
one background task, and dashboard reads shape dict_row output into JSON-safe
payloads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

import pytest

from hivemind.telemetry.ledger import (
    _COLUMN_ORDER,
    _insert_params,
    _QUEUE_MAX,
    _SQL_AGENTS,
    _SQL_DAILY,
    _SQL_LATENCY,
    _SQL_TOP_MODELS,
    NullLedger,
    TelemetryLedger,
    get_ledger,
    set_ledger,
)

_INSERT_MARK = "INSERT INTO mesh_telemetry.token_usage"


class FakeCursor:
    def __init__(self, conn, sql: str) -> None:
        self._conn = conn
        self._sql = sql

    async def fetchall(self):
        return list(self._conn.fetchall_rows.get(self._sql) or [])

    async def fetchone(self):
        return self._conn.fetchone_row


class FakeConn:
    """Records every execute(); returns canned rows keyed by exact SQL."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.autocommit = False
        self.closed = False
        self.fetchall_rows: dict[str, list[dict]] = {}
        self.fetchone_row: dict | None = None

    async def set_autocommit(self, value) -> None:
        self.autocommit = value

    async def execute(self, sql: str, params=None):
        self.executed.append((sql, params))
        return FakeCursor(self, sql)

    async def close(self) -> None:
        self.closed = True


def _fake_factory(conn: FakeConn):
    async def factory(dsn: str) -> FakeConn:
        return conn

    return factory


def _inserts(conn: FakeConn) -> list[tuple[str, tuple | None]]:
    return [(sql, params) for sql, params in conn.executed if _INSERT_MARK in sql]


# --- NullLedger / module singleton -------------------------------------------


async def test_null_ledger_noops():
    ledger = NullLedger()
    await ledger.record({"anything": 1})
    await ledger.connect()
    await ledger.shutdown()
    assert (await ledger.fetch_dashboard()) == {"error": "telemetry unavailable"}
    assert (await ledger.fetch_dashboard(days=30)) == {"error": "telemetry unavailable"}


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
    assert len(statements) == 5
    assert statements[0] == "CREATE SCHEMA IF NOT EXISTS mesh_telemetry"
    assert "CREATE TABLE IF NOT EXISTS mesh_telemetry.token_usage" in statements[1]
    assert "usage_cost" in statements[-1]
    assert conn.autocommit is True
    assert not conn.closed  # writer connection stays open for the next row


async def test_connect_failure_is_swallowed():
    async def boom(dsn):
        raise ConnectionError("pg down")

    ledger = TelemetryLedger("postgresql://fake", conn_factory=boom)
    await ledger.connect()  # must not raise (D4)


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


# --- read path (dashboard) ---------------------------------------------------


async def test_fetch_dashboard_shapes_payload_and_parameterizes_days():
    holder = FakeConn()
    day = date(2026, 9, 1)
    holder.fetchall_rows = {
        _SQL_DAILY: [
            {
                "day": day,
                "provider": "Anthropic",
                "requests": 10,
                "tokens_in": 1000,
                "tokens_out": 500,
                "cost_usd": 0.0123,
            },
            {
                "day": day,
                "provider": "Ollama (local)",
                "requests": 4,
                "tokens_in": 400,
                "tokens_out": 300,
                "cost_usd": 0.0,
            },
        ],
        _SQL_TOP_MODELS: [
            {
                "provider": "Anthropic",
                "model": "deepseek-chat",
                "requests": 9,
                "tokens_in": 900,
                "tokens_out": 400,
                "cost_usd": 0.0111,
            },
            {
                "provider": "Ollama (local)",
                "model": "llama3.2:1b",
                "requests": 4,
                "tokens_in": 400,
                "tokens_out": 300,
                "cost_usd": 0.0,
            },
        ],
        _SQL_AGENTS: [
            {
                "agent_hash": "bucket-1",
                "requests": 25,
                "tokens_in": 2500,
                "tokens_out": 1000,
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
    }
    holder.fetchone_row = {
        "requests": 14,
        "errors": 3,
        "tokens_in": 1400,
        "tokens_out": 800,
        "cost_usd": 0.0123,
        "local_requests": 4,
    }

    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))
    payload = await ledger.fetch_dashboard(days=7)

    assert payload["days"] == 7
    assert payload["generated_at"].endswith("+00:00")
    totals = payload["totals"]
    assert totals == {
        "requests": 14,
        "errors": 3,
        "error_rate": round(3 / 14, 4),
        "tokens_in": 1400,
        "tokens_out": 800,
        "cost_usd": 0.0123,
        "local_requests": 4,
        "local_share_pct": round(4 / 14 * 100.0, 2),
    }
    assert payload["daily"][0] == {
        "day": "2026-09-01",
        "provider": "Anthropic",
        "requests": 10,
        "tokens_in": 1000,
        "tokens_out": 500,
        "cost_usd": 0.0123,
    }
    assert payload["top_models"][0]["model"] == "deepseek-chat"
    assert payload["agents"] == [
        {
            "agent_hash": "bucket-1",
            "requests": 25,
            "tokens_in": 2500,
            "tokens_out": 1000,
            "cost_usd": 0.03,
            "errors": 3,
            "error_rate": 0.12,
        }
    ]
    assert payload["latency"][0]["p50_ms"] == 1234.5
    assert payload["latency"][1]["p50_ms"] is None
    # Everything must be JSON-serializable (no Decimal/date leakage).
    json.dumps(payload)

    # Every read query is parameterized on the day window.
    read_params = [params for sql, params in holder.executed if params is not None]
    assert len(read_params) == 5
    assert all(p == (7,) for p in read_params)
    # Schema DDL also ran on the reader connection (self-healing view).  The
    # multi-line statements begin with a newline, so match on content.
    ddl = [sql for sql, _ in holder.executed if "CREATE" in sql]
    assert len(ddl) == 5
    assert holder.closed is True, "short-lived reader connection must be closed"


async def test_fetch_dashboard_with_no_rows():
    holder = FakeConn()
    holder.fetchone_row = {
        "requests": 0,
        "errors": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "local_requests": 0,
    }
    ledger = TelemetryLedger("postgresql://fake", conn_factory=_fake_factory(holder))
    payload = await ledger.fetch_dashboard(days=14)
    assert payload["totals"] == {
        "requests": 0,
        "errors": 0,
        "error_rate": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "local_requests": 0,
        "local_share_pct": 0.0,
    }
    assert payload["daily"] == []
    assert payload["top_models"] == []
    assert payload["agents"] == []
    assert payload["latency"] == []
    json.dumps(payload)


async def test_fetch_dashboard_reader_failure_raises_for_handler():
    """The reader may raise; the HTTP handler owns mapping it to the
    'telemetry unavailable' payload (a real DB failure raises; only
    NullLedger returns the error payload directly)."""

    async def boom(dsn):
        raise ConnectionError("pg down")

    ledger = TelemetryLedger("postgresql://fake", conn_factory=boom)
    with pytest.raises(ConnectionError):
        await ledger.fetch_dashboard(days=7)
