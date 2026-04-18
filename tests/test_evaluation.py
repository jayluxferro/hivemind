"""Tests for the evaluation harness components."""

import asyncio
import json
import pytest
import httpx

from evaluation.mock_api import MockAPIConfig, MockAPIServer
from evaluation.mock_agent import AgentConfig, AgentResult, run_mock_agent
from evaluation.reporter import BenchmarkReport
from evaluation.harness import ScenarioResult


@pytest.fixture
async def mock_api():
    """Start a mock API server for testing."""
    import socket

    # Find free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    config = MockAPIConfig(port=port, requests_per_minute=100, base_latency_ms=10.0, latency_jitter_ms=5.0)
    server = MockAPIServer(config)
    task = asyncio.create_task(server.serve())

    # Wait for ready
    async with httpx.AsyncClient() as client:
        for _ in range(50):
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/_health", timeout=1.0)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ReadError):
                pass
            await asyncio.sleep(0.1)

    yield port, server

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, SystemExit):
        pass


@pytest.mark.asyncio
async def test_mock_api_health(mock_api):
    port, _ = mock_api
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"http://127.0.0.1:{port}/_health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_mock_api_messages(mock_api):
    port, _ = mock_api
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Hello"}],
            },
            headers={"x-api-key": "test", "content-type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert "usage" in data
        assert data["usage"]["input_tokens"] > 0
        assert data["usage"]["output_tokens"] > 0


@pytest.mark.asyncio
async def test_mock_api_rate_limit_headers(mock_api):
    port, _ = mock_api
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/v1/messages",
            json={"model": "test", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "test", "content-type": "application/json"},
        )
        assert "anthropic-ratelimit-requests-remaining" in resp.headers
        assert "anthropic-ratelimit-tokens-remaining" in resp.headers


@pytest.mark.asyncio
async def test_mock_api_stats(mock_api):
    port, _ = mock_api
    async with httpx.AsyncClient() as client:
        # Make a request first
        await client.post(
            f"http://127.0.0.1:{port}/v1/messages",
            json={"model": "test", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-api-key": "test"},
        )
        resp = await client.get(f"http://127.0.0.1:{port}/_stats")
        stats = resp.json()
        assert stats["total_requests"] >= 1


@pytest.mark.asyncio
async def test_mock_agent_success(mock_api):
    port, _ = mock_api
    config = AgentConfig(turns=3, message_length=50, inter_turn_delay_ms=10.0)
    result = await run_mock_agent(
        agent_id="test-agent",
        base_url=f"http://127.0.0.1:{port}",
        config=config,
    )
    assert result.alive is True
    assert result.requests_made == 3
    assert result.requests_succeeded == 3
    assert result.total_tokens > 0


@pytest.mark.asyncio
async def test_mock_agent_dies_on_error(mock_api):
    port, server = mock_api
    # Set 100% error rate
    server.config.error_rate = 1.0

    config = AgentConfig(turns=5, die_on_error=True, inter_turn_delay_ms=0)
    result = await run_mock_agent(
        agent_id="doomed-agent",
        base_url=f"http://127.0.0.1:{port}",
        config=config,
    )
    assert result.alive is False
    assert result.requests_failed > 0
    assert result.requests_made == 1  # Dies on first error

    # Reset for other tests
    server.config.error_rate = 0.0


def test_scenario_result_properties():
    r = ScenarioResult(
        scenario_name="test",
        mode="direct",
        started_at=100.0,
        completed_at=110.0,
    )
    r.agent_results = [
        AgentResult(agent_id="a1", alive=True, requests_made=10, requests_succeeded=10,
                    total_tokens_in=500, total_tokens_out=200, started_at=100, completed_at=105),
        AgentResult(agent_id="a2", alive=False, requests_made=5, requests_succeeded=3, requests_failed=2,
                    total_tokens_in=300, total_tokens_out=100, started_at=100, completed_at=103),
    ]
    assert r.agents_alive == 1
    assert r.agents_dead == 1
    assert r.failure_rate == 0.5
    assert r.total_tokens == 1100
    assert r.wasted_tokens == 400  # Dead agent's tokens
    assert r.wall_time_s == 10.0


def test_reporter_format():
    report = BenchmarkReport()
    direct = ScenarioResult(scenario_name="test", mode="direct", started_at=0, completed_at=5)
    direct.agent_results = [
        AgentResult(agent_id="a1", alive=True, started_at=0, completed_at=5,
                    total_tokens_in=100, total_tokens_out=50),
        AgentResult(agent_id="a2", alive=False, started_at=0, completed_at=3,
                    total_tokens_in=80, total_tokens_out=40, errors=["502"]),
    ]
    hm = ScenarioResult(scenario_name="test", mode="hivemind", started_at=0, completed_at=6)
    hm.agent_results = [
        AgentResult(agent_id="a1", alive=True, started_at=0, completed_at=6,
                    total_tokens_in=100, total_tokens_out=50),
        AgentResult(agent_id="a2", alive=True, started_at=0, completed_at=6,
                    total_tokens_in=100, total_tokens_out=50),
    ]

    report.add_comparison(direct, hm)
    table = report.format_table()
    assert "HiveMind Evaluation Results" in table
    assert "test" in table

    json_out = report.to_json()
    data = json.loads(json_out)
    assert len(data["comparisons"]) == 1
    assert data["comparisons"][0]["improvement"]["failure_rate_reduction_pp"] > 0
