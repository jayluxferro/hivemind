"""Dashboard endpoint tests — mocked ledger reader, no real Postgres (SPEC §6).

The routes live on the proxy's ASGI app and are served by the module-level
``get_ledger()``; tests swap in a fake reader via monkeypatch.  With no DSN
wired at startup the module default NullLedger answers "telemetry
unavailable", which is exactly the unconfigured behavior the proxy must keep.

Requests drive the ASGI app directly through httpx (no TestClient, no network).
"""

from __future__ import annotations

import json

import httpx
import pytest

from hivemind.proxy.server import ProxyServer
from hivemind.scheduler.admission import AdmissionController
from hivemind.scheduler.backpressure import BackpressureController
from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.storage.models import HiveMindConfig

PAGE_MARKERS = (
    b"token ledger",
    b"/_telemetry/data",
    b"Telemetry unavailable",
    b"seed_pricing.sql",
    b"<script",
    b"<style",
)


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
    """Async ledger reader stand-in: records the days it was asked for."""

    def __init__(self, payload: dict | None = None, error: Exception | None = None) -> None:
        self.payload = payload if payload is not None else {}
        self.error = error
        self.seen_days: list[int] = []

    async def fetch_dashboard(self, days: int = 14):
        self.seen_days.append(days)
        if self.error is not None:
            raise self.error
        return self.payload


async def test_telemetry_page_is_self_contained_html(proxy):
    resp = await _get(proxy, "/_telemetry")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.content
    for marker in PAGE_MARKERS:
        assert marker in body, f"page missing marker {marker!r}"
    lower = body.lower()
    # No external assets (SPEC: no CDN, offline-only page).
    for external in (b"cdnjs", b"unpkg", b"jsdelivr", b"//fonts", b"https://"):
        assert external not in lower, f"page references external asset {external!r}"


async def test_telemetry_data_default_days_and_payload_passthrough(proxy, monkeypatch):
    payload = {
        "days": 14,
        "generated_at": "2026-09-03T00:00:00+00:00",
        "totals": {"requests": 1, "errors": 0, "error_rate": 0.0},
        "daily": [],
        "top_models": [],
        "agents": [],
        "latency": [],
    }
    reader = FakeReader(payload=payload)
    monkeypatch.setattr("hivemind.proxy.server.get_ledger", lambda: reader)

    resp = await _get(proxy, "/_telemetry/data")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == payload
    assert reader.seen_days == [14]
    assert resp.headers["cache-control"] == "no-store"


async def test_telemetry_data_honors_and_clamps_days(proxy, monkeypatch):
    reader = FakeReader(payload={"days": 0})
    monkeypatch.setattr("hivemind.proxy.server.get_ledger", lambda: reader)

    await _get(proxy, "/_telemetry/data", params={"days": 7})
    await _get(proxy, "/_telemetry/data", params={"days": 99999})  # clamp to 365
    await _get(proxy, "/_telemetry/data", params={"days": 0})  # clamp to 1
    await _get(proxy, "/_telemetry/data", params={"days": "not-a-number"})  # default
    assert reader.seen_days == [7, 365, 1, 14]


async def test_telemetry_data_db_failure_is_200_unavailable(proxy, monkeypatch):
    reader = FakeReader(error=RuntimeError("pg down"))
    monkeypatch.setattr("hivemind.proxy.server.get_ledger", lambda: reader)

    resp = await _get(proxy, "/_telemetry/data")
    assert resp.status_code == 200
    assert resp.json() == {"error": "telemetry unavailable"}


async def test_telemetry_data_unconfigured_ledger_is_unavailable(proxy):
    # No monkeypatch: module get_ledger() still returns the NullLedger default
    # because no DSN was wired — the proxy behaves exactly as before telemetry.
    resp = await _get(proxy, "/_telemetry/data", params={"days": 7})
    assert resp.status_code == 200
    assert resp.json() == {"error": "telemetry unavailable"}


async def test_telemetry_data_is_valid_json(proxy, monkeypatch):
    reader = FakeReader(payload={"days": 14, "totals": {}, "daily": [], "top_models": [], "agents": [], "latency": []})
    monkeypatch.setattr("hivemind.proxy.server.get_ledger", lambda: reader)
    resp = await _get(proxy, "/_telemetry/data")
    json.loads(resp.text)  # must not raise
