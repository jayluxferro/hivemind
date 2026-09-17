"""Telemetry endpoint tests — mocked ledger reader, no real Postgres (SPEC §6).

The routes live on the proxy's ASGI app and are served by the module-level
``get_ledger()``; tests swap in a fake reader via monkeypatch.  With no DSN
wired at startup the module default NullLedger answers "telemetry
unavailable", which is exactly the unconfigured behavior the proxy must keep.

Requests drive the ASGI app directly through httpx (no TestClient, no
network).  The fake reader records *what the handler asked for* — the range,
the bucket, the filters — because that translation (URL params -> ledger
kwargs) is the whole job of these handlers, and the paranoia targets are the
places user input could otherwise reach SQL: sort/order/bucket/filename.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from hivemind.proxy.server import ProxyServer
from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController
from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.storage.models import HiveMindConfig
from hivemind.telemetry.dashboard import STATIC_ASSETS, STATIC_VERSION, read_static, render_page
from hivemind.telemetry.ledger import _REQUEST_COLUMNS

# The shell is a document, not a program: it must reference the three assets
# (content-hashed) and carry no inline CSS/JS of its own.
PAGE_MARKERS = (
    b"token ledger",
    b'<link rel="stylesheet" href="/_telemetry/static/dashboard.css?v=',
    b'<script defer src="/_telemetry/static/charts.js?v=',
    b'<script defer src="/_telemetry/static/dashboard.js?v=',
)

# The assets are real programs, not the scaffolds they replaced.  These markers
# pin the promises the SPEC makes about them (frontend spec + D11): a
# strict-mode program, the one page-wide palette, the opt-in refresh preference
# in localStorage, the single error state, and the toolkit global the shell's
# first script is expected to publish.  "use strict" is owed by BOTH JS files —
# a dropped directive is exactly the kind of regression that only shows up as a
# sloppy global months later.
CONTENT_MARKERS: dict[str, tuple[bytes, ...]] = {
    "charts.js": (b'"use strict"', b"window.HiveCharts", b"PALETTE", b"ColorBrewer Set2"),
    "dashboard.js": (
        b'"use strict"',
        b"localStorage",
        b"hivemindTelemetryAutoRefresh",
        b"Telemetry unavailable",
        b"fail-open",
    ),
    # The stylesheet carries the Phase 1 surface and the Phase 2 chrome the
    # frontend spec names out loud (nav tabs, the detail side panel).
    "dashboard.css": (b"#111", b".tabs", b".panel"),
}

# Anything here anywhere in a served byte payload means the page is not
# offline-only (SPEC D11).  The SVG namespace is not in this list on purpose:
# "http://www.w3.org/2000/svg" is an XML identifier that is never fetched, and
# the scan targets the literal "https://" plus the three CDNs and the protocol
# -relative font trick.
EXTERNAL_MARKERS = (b"cdnjs", b"unpkg", b"jsdelivr", b"//fonts", b"https://")

_ASSET_TYPES = {
    "dashboard.css": "text/css; charset=utf-8",
    "charts.js": "text/javascript; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}

_MODEL_INDEX = _REQUEST_COLUMNS.index("model")

_UNAVAILABLE = {"error": "telemetry unavailable"}


def _row(**overrides) -> dict:
    """A shaped request row as the ledger would hand it to the export writer."""
    row = {
        "id": 1,
        "ts": "2026-09-02T03:04:05Z",
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


@pytest.fixture
def proxy() -> ProxyServer:
    config = HiveMindConfig(
        proxy_host="127.0.0.1",
        proxy_port=0,
        upstream_url="http://test-upstream",
        max_retries=2,
        retry_base_delay=0.01,
        retry_max_delay=0.05,
        max_concurrency=5,
        # telemetry_dsn defaults to None: no ledger wiring at startup
    )
    return ProxyServer(
        config=config,
        admission=AdmissionController(5),
        rate_limiter=RateLimiter(),
        backpressure=BackpressureController(5),
        budget_manager=BudgetManager(),
        db=None,
    )


async def _get(proxy: ProxyServer, path: str, params: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=proxy.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        return await client.get(path, params=params)


class FakeReader:
    """Async ledger reader stand-in: records every call it receives.

    ``calls`` is a list of ``(method, args, kwargs)`` so a test can assert the
    exact range/filters a handler derived for one URL.  ``error`` makes every
    method raise (the fail-open path); ``rows`` feeds the export stream.
    """

    def __init__(self, payload: dict | None = None, error: Exception | None = None, rows: list | None = None):
        self.payload = payload if payload is not None else {}
        self.error = error
        self.rows = rows or []
        self.count = len(self.rows)
        self.calls: list[tuple[str, tuple, dict]] = []
        self.export_fail = False

    def _record(self, name: str, args: tuple, kwargs: dict):
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error
        return self.payload

    def only_call(self, name: str) -> tuple[tuple, dict]:
        """The single call to ``name`` — asserts there was exactly one."""
        matching = [call for call in self.calls if call[0] == name]
        assert len(matching) == 1, f"expected one {name} call, got {self.calls}"
        return matching[0][1], matching[0][2]

    async def fetch_overview(self, from_ts, to_ts):
        return self._record("fetch_overview", (from_ts, to_ts), {})

    async def fetch_requests(self, from_ts, to_ts, **kwargs):
        return self._record("fetch_requests", (from_ts, to_ts), kwargs)

    async def fetch_facets(self, from_ts, to_ts):
        return self._record("fetch_facets", (from_ts, to_ts), {})

    async def fetch_series(self, from_ts, to_ts, bucket, **kwargs):
        return self._record("fetch_series", (from_ts, to_ts, bucket), kwargs)

    async def count_requests(self, from_ts, to_ts, *, filters=None):
        self._record("count_requests", (from_ts, to_ts), {"filters": filters})
        return self.count

    async def export_rows(self, from_ts, to_ts, *, filters=None, **kwargs):
        self._record("export_rows", (from_ts, to_ts), {"filters": filters})
        if self.export_fail:
            raise RuntimeError("pg vanished mid-stream")
        if self.rows:
            yield self.rows


def _use(monkeypatch, reader: FakeReader) -> FakeReader:
    monkeypatch.setattr("hivemind.proxy.server.get_ledger", lambda: reader)
    return reader


def _csv_rows(resp: httpx.Response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(resp.text, newline="")))


# --- shell + static assets ----------------------------------------------------


async def test_telemetry_page_is_a_shell_of_versioned_local_assets(proxy):
    resp = await _get(proxy, "/_telemetry")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"
    body = resp.content
    for marker in PAGE_MARKERS:
        assert marker in body, f"page missing marker {marker!r}"
    assert f'?v={STATIC_VERSION}"'.encode() in body  # cache-busted, not bare
    # The shell is a document: no inline style block, no inline handlers.
    assert b"<style" not in body.lower()
    assert b"onclick" not in body.lower()
    # No external assets (SPEC: no CDN, offline-only page).
    lower = body.lower()
    for external in EXTERNAL_MARKERS:
        assert external not in lower, f"page references external asset {external!r}"


def _served_payloads() -> dict[str, bytes]:
    """Every byte the dashboard can hand a browser: the shell plus all assets."""
    payloads = {"shell": render_page().encode()}
    for name in sorted(STATIC_ASSETS):
        asset = read_static(name)
        assert asset is not None, f"{name} is whitelisted but unreadable"
        payloads[name] = asset[0]
    return payloads


@pytest.mark.parametrize("name", sorted(CONTENT_MARKERS))
def test_static_assets_carry_the_frontend_contract(name):
    asset = read_static(name)
    assert asset is not None, f"{name} is whitelisted but unreadable"
    body = asset[0]
    for marker in CONTENT_MARKERS[name]:
        assert marker in body, f"{name} is missing {marker!r}"


def test_no_external_assets_in_any_served_bytes():
    # Offline-only is a property of the whole delivery, not of the shell alone:
    # a CDN link in a stylesheet or a remote font in the JS would be just as
    # broken in an air-gapped deployment (SPEC D11).
    for name, body in _served_payloads().items():
        lower = body.lower()
        for external in EXTERNAL_MARKERS:
            assert external not in lower, f"{name} references external asset {external!r}"


def test_assets_are_wired_to_each_other_not_to_a_version_string():
    # Every asset the shell names is whitelisted and readable, and the toolkit
    # global the second script expects is published by the first.
    shell = render_page()
    for name in STATIC_ASSETS:
        assert f"/_telemetry/static/{name}?v={STATIC_VERSION}" in shell, f"shell does not load {name}"
    charts = read_static("charts.js")
    dashboard = read_static("dashboard.js")
    assert charts is not None and dashboard is not None
    assert b"window.HiveCharts" in charts[0]
    assert b"window.HiveCharts" in dashboard[0]  # the consumer reads the same global


@pytest.mark.parametrize("name", sorted(_ASSET_TYPES))
async def test_static_assets_are_served_with_their_content_types(proxy, name):
    resp = await _get(proxy, f"/_telemetry/static/{name}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == _ASSET_TYPES[name]
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert len(resp.content) > 100  # a real file, not an empty placeholder
    asset = read_static(name)
    assert asset is not None and resp.content == asset[0]


async def test_unknown_static_name_is_404(proxy):
    resp = await _get(proxy, "/_telemetry/static/robots.txt")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not found"}


def test_read_static_never_reaches_outside_the_package():
    # The whitelist is the guard: these are dict lookups, so a traversal is
    # simply not a key.  (Tested here, not over HTTP: URL normalization in the
    # client/ASGI layer rewrites the dots before a route ever sees them.)
    for name in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "dashboard.css/../../setup.py", "", "static"):
        assert read_static(name) is None


def test_static_version_is_a_content_hash():
    assert len(STATIC_VERSION) == 8
    assert all(char in "0123456789abcdef" for char in STATIC_VERSION)


# --- /_telemetry/data ---------------------------------------------------------


async def test_telemetry_data_defaults_to_the_last_two_weeks(proxy, monkeypatch):
    payload = {"from": "x", "to": "y", "totals": {}, "rows": []}
    reader = _use(monkeypatch, FakeReader(payload=payload))

    resp = await _get(proxy, "/_telemetry/data")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == payload
    (from_ts, to_ts), _ = reader.only_call("fetch_overview")
    assert abs((datetime.now(timezone.utc) - to_ts).total_seconds()) < 60
    assert abs((to_ts - from_ts) - timedelta(days=14)) < timedelta(minutes=1)


async def test_telemetry_data_honors_explicit_range(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())

    await _get(proxy, "/_telemetry/data", params={"from": "2026-09-01", "to": "2026-09-05"})

    (from_ts, to_ts), _ = reader.only_call("fetch_overview")
    assert from_ts == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert to_ts == datetime(2026, 9, 5, tzinfo=timezone.utc)  # inclusive, not +1d


async def test_telemetry_data_days_alias_still_works_and_clamps(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())
    now = datetime.now(timezone.utc)

    await _get(proxy, "/_telemetry/data", params={"days": 7})
    await _get(proxy, "/_telemetry/data", params={"days": 99999})  # clamp to 365
    await _get(proxy, "/_telemetry/data", params={"days": 0})  # clamp to 1
    await _get(proxy, "/_telemetry/data", params={"days": "not-a-number"})  # default

    windows = [args for name, args, _ in reader.calls if name == "fetch_overview"]
    spans = [round((to_ts - from_ts) / timedelta(days=1)) for from_ts, to_ts in windows]
    assert spans == [7, 365, 1, 14]
    assert all(abs((now - to_ts).total_seconds()) < 60 for _, to_ts in windows)


async def test_telemetry_data_explicit_range_beats_days(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())

    await _get(proxy, "/_telemetry/data", params={"days": 30, "from": "2026-09-01", "to": "2026-09-05"})

    (from_ts, to_ts), _ = reader.only_call("fetch_overview")
    assert from_ts == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert to_ts == datetime(2026, 9, 5, tzinfo=timezone.utc)


async def test_telemetry_data_clamps_future_and_oversized_ranges(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())
    now = datetime.now(timezone.utc)

    await _get(proxy, "/_telemetry/data", params={"from": "2020-01-01", "to": "2030-01-01"})
    await _get(proxy, "/_telemetry/data", params={"from": "2026-09-10", "to": "2026-09-01"})
    await _get(proxy, "/_telemetry/data", params={"from": "garbage", "to": "garbage"})

    windows = [args for name, args, _ in reader.calls if name == "fetch_overview"]
    future_from, future_to = windows[0]
    assert future_to <= now + timedelta(seconds=1)  # a future `to` is not a window into nothing
    assert (future_to - future_from) <= timedelta(days=366)
    inverted_from, inverted_to = windows[1]
    assert inverted_from <= inverted_to  # inverted pair swaps, never spans -9d
    garbage_from, garbage_to = windows[2]
    assert abs((garbage_to - garbage_from) - timedelta(days=14)) < timedelta(minutes=1)


async def test_telemetry_data_db_failure_is_200_unavailable(proxy, monkeypatch):
    _use(monkeypatch, FakeReader(error=RuntimeError("pg down")))

    resp = await _get(proxy, "/_telemetry/data")

    assert resp.status_code == 200
    assert resp.json() == _UNAVAILABLE


async def test_telemetry_data_unconfigured_ledger_is_unavailable(proxy):
    # No monkeypatch: module get_ledger() still returns the NullLedger default
    # because no DSN was wired — the proxy behaves exactly as before telemetry.
    resp = await _get(proxy, "/_telemetry/data", params={"days": 7})
    assert resp.status_code == 200
    assert resp.json() == _UNAVAILABLE


# --- /_telemetry/requests -----------------------------------------------------


async def test_telemetry_requests_passes_validated_filters_through(proxy, monkeypatch):
    payload = {"total": 0, "limit": 10, "offset": 0, "from": "x", "to": "y", "rows": []}
    reader = _use(monkeypatch, FakeReader(payload=payload))

    resp = await _get(
        proxy,
        "/_telemetry/requests",
        params={
            "from": "2026-09-01",
            "to": "2026-09-05",
            "agent_hash": "bucket-1",
            "model": "deepseek-chat",
            "provider": "Anthropic",
            "status": "500",
            "sort": "tokens",
            "order": "asc",
            "limit": "10",
            "offset": "20",
        },
    )

    assert resp.status_code == 200
    assert resp.json() == payload
    (from_ts, to_ts), kwargs = reader.only_call("fetch_requests")
    assert from_ts == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert to_ts == datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert kwargs == {
        "agent_hash": "bucket-1",
        "model": "deepseek-chat",
        "provider": "Anthropic",
        "status": 500,
        "sort": "tokens",
        "order": "asc",
        "limit": 10,
        "offset": 20,
    }


async def test_telemetry_requests_clamps_and_defaults(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())

    await _get(proxy, "/_telemetry/requests", params={"limit": "99999", "offset": "-5", "status": "999"})
    await _get(
        proxy,
        "/_telemetry/requests",
        params={"sort": "ts; DROP TABLE token_usage", "order": "DESC; --", "status": "abc"},
    )

    first = reader.calls[0][2]
    assert first["limit"] == 500  # clamped to MAX_LIMIT
    assert first["offset"] == 0  # floor
    assert "status" not in first  # 999 is not an HTTP status
    second = reader.calls[1][2]
    assert second["sort"] == "ts"  # whitelist fallback, never interpolated
    assert second["order"] == "desc"  # lower-cased through the whitelist
    assert "status" not in second


async def test_telemetry_requests_db_failure_is_200_unavailable(proxy, monkeypatch):
    _use(monkeypatch, FakeReader(error=RuntimeError("pg down")))
    resp = await _get(proxy, "/_telemetry/requests")
    assert resp.status_code == 200
    assert resp.json() == _UNAVAILABLE


# --- /_telemetry/facets -------------------------------------------------------


async def test_telemetry_facets_passes_the_range(proxy, monkeypatch):
    payload = {"agents": [], "models": [], "providers": []}
    reader = _use(monkeypatch, FakeReader(payload=payload))

    resp = await _get(proxy, "/_telemetry/facets", params={"from": "2026-09-01", "to": "2026-09-05"})

    assert resp.status_code == 200
    assert resp.json() == payload
    (from_ts, to_ts), _ = reader.only_call("fetch_facets")
    assert (from_ts, to_ts) == (datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 5, tzinfo=timezone.utc))


async def test_telemetry_facets_db_failure_is_200_unavailable(proxy, monkeypatch):
    _use(monkeypatch, FakeReader(error=RuntimeError("pg down")))
    resp = await _get(proxy, "/_telemetry/facets")
    assert resp.status_code == 200
    assert resp.json() == _UNAVAILABLE


# --- /_telemetry/series -------------------------------------------------------


async def test_telemetry_series_defaults_to_hour_and_drops_paging_params(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())

    await _get(
        proxy,
        "/_telemetry/series",
        params={
            "from": "2026-09-13",
            "to": "2026-09-14",
            "agent_hash": "bucket-1",
            "model": "deepseek-chat",
            "provider": "Anthropic",
            "status": "200",  # series rows are aggregated: status is ignored
            "limit": "5",  # so are paging/sort params
            "sort": "tokens",
        },
    )

    (_, _, bucket), kwargs = reader.only_call("fetch_series")
    assert bucket == "hour"
    assert kwargs == {"agent_hash": "bucket-1", "model": "deepseek-chat", "provider": "Anthropic"}


async def test_telemetry_series_degrades_hour_to_day_for_long_ranges(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader())

    await _get(proxy, "/_telemetry/series", params={"bucket": "hour"})  # 14 days -> hour
    await _get(
        proxy,
        "/_telemetry/series",
        params={"from": "2026-08-01", "to": "2026-09-14", "bucket": "hour"},  # 44 days -> day
    )
    await _get(proxy, "/_telemetry/series", params={"bucket": "day"})  # explicit day stays
    await _get(proxy, "/_telemetry/series", params={"bucket": "minute; DROP"})  # unknown -> hour

    buckets = [args[2] for name, args, _ in reader.calls if name == "fetch_series"]
    assert buckets == ["hour", "day", "day", "hour"]


async def test_telemetry_series_db_failure_is_200_unavailable(proxy, monkeypatch):
    _use(monkeypatch, FakeReader(error=RuntimeError("pg down")))
    resp = await _get(proxy, "/_telemetry/series")
    assert resp.status_code == 200
    assert resp.json() == _UNAVAILABLE


# --- export -------------------------------------------------------------------


async def test_export_csv_header_rows_and_injection_guard(proxy, monkeypatch):
    _use(
        monkeypatch,
        FakeReader(
            rows=[
                _row(),
                _row(id=2, model="=SUM(A1:A9)", agent_hash="+1+1", provider="@cmd"),
                _row(id=3, model="-2+3", agent_hash="\tlead", provider="\rlead"),
            ]
        ),
    )

    resp = await _get(proxy, "/_telemetry/export.csv", params={"from": "2026-09-01", "to": "2026-09-05"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/csv; charset=utf-8"
    assert resp.headers["content-disposition"] == 'attachment; filename="hivemind-telemetry-2026-09-01--2026-09-05.csv"'
    assert resp.headers["cache-control"] == "no-store"
    # RFC 4180 line endings, header first, nothing but CRLF as separator.
    assert resp.content.endswith(b"\r\n")
    assert b"\n" not in resp.content.replace(b"\r\n", b"")
    rows = _csv_rows(resp)
    assert rows[0] == list(_REQUEST_COLUMNS)
    assert len(rows) == 4
    # Cells are guarded before quoting, so a formula-ish value can never be
    # the first thing a spreadsheet sees.
    assert rows[1][_MODEL_INDEX] == "deepseek-chat"  # benign value untouched
    assert rows[2][_MODEL_INDEX] == "'=SUM(A1:A9)"
    assert rows[2][_REQUEST_COLUMNS.index("agent_hash")] == "'+1+1"
    assert rows[2][_REQUEST_COLUMNS.index("provider")] == "'@cmd"
    assert rows[3][_MODEL_INDEX] == "'-2+3"
    assert rows[3][_REQUEST_COLUMNS.index("agent_hash")] == "'\tlead"
    assert rows[3][_REQUEST_COLUMNS.index("provider")] == "'\rlead"
    # A missing cell stays empty rather than becoming the string "None".
    assert rows[1][_REQUEST_COLUMNS.index("cache_read")] == "40"
    assert rows[1][_REQUEST_COLUMNS.index("latency_ms")] == "123.4"


async def test_export_csv_none_cells_are_empty(proxy, monkeypatch):
    _use(monkeypatch, FakeReader(rows=[_row(tokens_in=None, cache_read=None, latency_ms=None, status=503)]))

    resp = await _get(proxy, "/_telemetry/export.csv")

    cells = _csv_rows(resp)[1]
    assert cells[_REQUEST_COLUMNS.index("tokens_in")] == ""
    assert cells[_REQUEST_COLUMNS.index("cache_read")] == ""
    assert cells[_REQUEST_COLUMNS.index("latency_ms")] == ""
    assert cells[_REQUEST_COLUMNS.index("status")] == "503"


async def test_export_jsonl_is_one_compact_object_per_line(proxy, monkeypatch):
    rows = [_row(), _row(id=2, model='{"nested": "json"}')]
    _use(monkeypatch, FakeReader(rows=rows))

    resp = await _get(proxy, "/_telemetry/export.jsonl", params={"from": "2026-09-01", "to": "2026-09-05"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["content-disposition"].endswith('.jsonl"')
    lines = resp.text.splitlines()
    assert len(lines) == 2
    for line, row in zip(lines, rows, strict=True):
        assert json.loads(line) == row  # raw values: no CSV guard belongs here
    # Compact separators (checked on the line whose *values* contain none).
    assert ", " not in lines[0] and ": " not in lines[0]
    assert '"model":"{\\"nested\\": \\"json\\"}"' in lines[1]  # value kept verbatim, escaped once


async def test_export_preflight_failure_is_200_unavailable(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader(error=RuntimeError("pg down"), rows=[_row()]))

    resp = await _get(proxy, "/_telemetry/export.csv")

    # The failure happens before a byte of body, so the client gets the same
    # JSON marker every other endpoint uses — not a truncated 200 stream.
    assert resp.status_code == 200
    assert resp.json() == _UNAVAILABLE
    assert not any(name == "export_rows" for name, _, _ in reader.calls)


async def test_export_midstream_failure_truncates_cleanly(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader(rows=[_row()]))
    reader.export_fail = True  # count succeeds, the cursor blows up later

    resp = await _get(proxy, "/_telemetry/export.jsonl")

    assert resp.status_code == 200  # status was committed by the pre-flight
    assert resp.text == ""  # documented truncation, not an ASGI error
    assert any(name == "export_rows" for name, _, _ in reader.calls)


async def test_export_passes_filters_to_both_the_count_and_the_stream(proxy, monkeypatch):
    reader = _use(monkeypatch, FakeReader(rows=[_row()]))

    await _get(
        proxy,
        "/_telemetry/export.csv",
        params={"from": "2026-09-01", "to": "2026-09-05", "agent_hash": "bucket-1", "status": "500"},
    )

    _, count_kwargs = reader.only_call("count_requests")
    assert count_kwargs == {
        "filters": {
            "agent_hash": "bucket-1",
            "status": 500,
            "sort": "ts",
            "order": "desc",
            "limit": 100,
            "offset": 0,
        }
    }
    (from_ts, to_ts), export_kwargs = reader.only_call("export_rows")
    assert (from_ts, to_ts) == (datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert export_kwargs["filters"] == {
        "agent_hash": "bucket-1",
        "status": 500,
        "sort": "ts",
        "order": "desc",
        "limit": 100,
        "offset": 0,
    }
