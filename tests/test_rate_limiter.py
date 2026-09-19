"""Tests for the rate limit tracker."""

import time

import pytest

from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.scheduler.providers import ANTHROPIC, OPENAI, OLLAMA


@pytest.mark.asyncio
async def test_initial_state():
    rl = RateLimiter()
    assert not rl.is_throttled
    assert rl.throttle_remaining_seconds == 0.0
    assert rl.get_window("default") is None


@pytest.mark.asyncio
async def test_parse_anthropic_headers():
    rl = RateLimiter()
    headers = {
        "anthropic-ratelimit-requests-remaining": "45",
        "anthropic-ratelimit-tokens-remaining": "80000",
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-tokens-limit": "100000",
    }
    await rl.update_from_headers(headers, provider="anthropic")

    window = rl.get_window("anthropic")
    assert window is not None
    assert window.remaining_requests == 45
    assert window.remaining_tokens == 80000
    assert window.limit_requests == 50
    assert window.limit_tokens == 100000


@pytest.mark.asyncio
async def test_parse_openai_headers():
    rl = RateLimiter()
    headers = {
        "x-ratelimit-remaining-requests": "20",
        "x-ratelimit-remaining-tokens": "50000",
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-limit-tokens": "100000",
    }
    await rl.update_from_headers(headers, provider="openai")

    window = rl.get_window("openai")
    assert window is not None
    assert window.remaining_requests == 20
    assert window.remaining_tokens == 50000


@pytest.mark.asyncio
async def test_retry_after_header():
    rl = RateLimiter()
    headers = {"retry-after": "3.0"}
    await rl.update_from_headers(headers)

    assert rl.is_throttled
    assert rl.throttle_remaining_seconds > 2.0


@pytest.mark.asyncio
async def test_proactive_throttle():
    rl = RateLimiter()
    headers = {
        "anthropic-ratelimit-requests-remaining": "1",
        "anthropic-ratelimit-requests-limit": "50",
    }
    await rl.update_from_headers(headers)
    # Should have triggered proactive throttle
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_wait_if_throttled():
    rl = RateLimiter()
    # Not throttled — should return immediately
    waited = await rl.wait_if_throttled()
    assert waited == 0.0


@pytest.mark.asyncio
async def test_wait_cap_raises_throttle_wait_exceeded():
    """Regression: an unbounded queue once stretched to ~300s — every
    layer's read ceiling — surfacing as a bare gateway ReadTimeout with
    zero downstream logs (2026-09-01).  A projected wait beyond MAX_WAIT_S
    must raise so the interceptor can fail fast with a 429."""
    from hivemind.scheduler.rate_limiter import MAX_WAIT_S, ThrottleWaitExceeded

    rl = RateLimiter()
    rl._wait_seconds = lambda agent_id: MAX_WAIT_S + 1.0  # simulate deep queue
    with pytest.raises(ThrottleWaitExceeded):
        await rl.wait_if_throttled()


@pytest.mark.asyncio
async def test_custom_max_wait_cap_honored():
    """The queue cap is per-instance, so operators can tune the hold time."""
    from hivemind.scheduler.rate_limiter import ThrottleWaitExceeded

    rl = RateLimiter(max_wait_s=10.0)
    rl._wait_seconds = lambda agent_id: 11.0  # beyond the custom cap
    with pytest.raises(ThrottleWaitExceeded) as exc_info:
        await rl.wait_if_throttled()
    assert exc_info.value.cap_s == 10.0


@pytest.mark.asyncio
async def test_wait_within_cap_holds_and_proceeds(monkeypatch):
    """A queue wait inside the cap must hold (not 429) and then proceed.

    This is the behavior that keeps coding agents alive: their request
    stays open until a slot frees instead of erroring out.
    """
    rl = RateLimiter(max_wait_s=5.0)
    calls = {"n": 0}

    def fake_wait(agent_id=None):
        calls["n"] += 1
        return 1.0 if calls["n"] == 1 else 0.0  # wait once, then clear

    rl._wait_seconds = fake_wait
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("hivemind.scheduler.rate_limiter.asyncio.sleep", fake_sleep)
    waited = await rl.wait_if_throttled()
    assert waited == 1.0
    assert slept == [1.0]
    assert rl.stats["rpm_current"] == 1  # request recorded after the hold


@pytest.mark.asyncio
async def test_invalid_max_wait_rejected():
    with pytest.raises(ValueError):
        RateLimiter(max_wait_s=0)
    with pytest.raises(ValueError):
        RateLimiter(max_wait_s=-5)
    with pytest.raises(ValueError):
        RateLimiter(max_wait_s="lots")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_stats():
    rl = RateLimiter()
    stats = rl.stats
    assert "is_throttled" in stats
    assert "providers" in stats
    assert isinstance(stats["providers"], dict)


# --- Provider profile tests ---


@pytest.mark.asyncio
async def test_configure_from_profile():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)
    assert rl._rpm_limit == 50
    assert rl._tpm_limit == 80_000
    assert rl._provider_name == "Anthropic"

    stats = rl.stats
    assert stats["provider"] == "Anthropic"
    assert stats["rpm_limit"] == 50
    assert stats["rpm_current"] == 0


@pytest.mark.asyncio
async def test_rpm_throttle():
    """RPM counter should throttle when requests hit the limit."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)  # 50 RPM

    # Record 50 requests — should hit the limit
    for _ in range(50):
        rl.record_request()

    assert rl._rpm_wait_seconds() > 0
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_rpm_no_throttle_below_limit():
    rl = RateLimiter()
    rl.configure_from_profile(OPENAI)  # 60 RPM

    for _ in range(30):
        rl.record_request()

    assert rl._rpm_wait_seconds() == 0.0
    assert not rl.is_throttled


@pytest.mark.asyncio
async def test_tpm_throttle():
    """TPM counter should throttle when token usage hits the limit."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)  # 80K TPM

    rl.record_tokens(80_000)
    assert rl._tpm_wait_seconds() > 0
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_wait_records_request():
    """wait_if_throttled should record the request in the RPM window."""
    rl = RateLimiter()
    rl.configure_from_profile(OPENAI)

    await rl.wait_if_throttled()
    assert rl.stats["rpm_current"] == 1


@pytest.mark.asyncio
async def test_no_profile_no_rpm_throttle():
    """Without a profile, RPM/TPM enforcement is disabled."""
    rl = RateLimiter()
    for _ in range(1000):
        rl.record_request()
    assert rl._rpm_wait_seconds() == 0.0
    assert not rl.is_throttled


@pytest.mark.asyncio
async def test_stats_include_provider_fields():
    rl = RateLimiter()
    rl.configure_from_profile(OLLAMA)

    rl.record_request()
    rl.record_tokens(500)

    stats = rl.stats
    assert stats["provider"] == "Ollama (local)"
    assert stats["rpm_limit"] == 1000
    assert stats["rpm_current"] == 1
    assert stats["tpm_current"] == 500


# --- Per-agent scope tests ---


@pytest.mark.asyncio
async def test_per_agent_windows_are_isolated():
    """One agent saturating its window must not throttle another agent."""
    rl = RateLimiter()  # default scope="per_agent"
    rl.configure_from_profile(ANTHROPIC)  # 50 RPM

    for _ in range(50):
        rl.record_request(agent_id="agent-a")

    assert rl._rpm_wait_seconds(agent_id="agent-a") > 0
    assert rl._rpm_wait_seconds(agent_id="agent-b") == 0.0
    assert rl.agent_is_throttled("agent-a")
    assert not rl.agent_is_throttled("agent-b")
    # Global view stays clean: nothing was recorded without an identity
    assert not rl.is_throttled


@pytest.mark.asyncio
async def test_per_agent_tpm_isolated():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)  # 80K TPM

    rl.record_tokens(80_000, agent_id="agent-a")

    assert rl._tpm_wait_seconds(agent_id="agent-a") > 0
    assert rl._tpm_wait_seconds(agent_id="agent-b") == 0.0


@pytest.mark.asyncio
async def test_global_scope_shares_window():
    """scope="global" keeps the original one-shared-window behavior."""
    rl = RateLimiter(scope="global")
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(50):
        rl.record_request(agent_id="agent-a")

    assert rl._rpm_wait_seconds(agent_id="agent-b") > 0
    assert rl.is_throttled


@pytest.mark.asyncio
async def test_unidentified_calls_use_global_window():
    """Calls without an agent_id keep the pre-feature behavior..."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(50):
        rl.record_request()

    assert rl._rpm_wait_seconds() > 0
    # ...but an identified agent is not dragged down by the shared window
    assert rl._rpm_wait_seconds(agent_id="agent-a") == 0.0


@pytest.mark.asyncio
async def test_header_pause_is_global_across_agents():
    """retry-after reflects the shared API key — it pauses every agent."""
    rl = RateLimiter()
    await rl.update_from_headers({"retry-after": "3.0"})

    assert rl.agent_is_throttled("agent-a")
    assert rl.agent_is_throttled("agent-b")


@pytest.mark.asyncio
async def test_wait_if_throttled_records_in_agent_window():
    rl = RateLimiter()
    rl.configure_from_profile(OPENAI)

    await rl.wait_if_throttled(agent_id="agent-a")

    stats = rl.stats
    assert stats["agents"]["agent-a"]["rpm_current"] == 1
    # Global window untouched by identified traffic in per-agent scope
    assert stats["rpm_current"] == 0


@pytest.mark.asyncio
async def test_set_scope_switches_windowing():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(50):
        rl.record_request(agent_id="agent-a")

    rl.set_scope("global")
    # Global window is fresh — agent-a's per-agent traffic never touched it
    assert rl._rpm_wait_seconds(agent_id="agent-b") == 0.0

    rl.set_scope("per_agent")
    assert rl._rpm_wait_seconds(agent_id="agent-a") > 0


@pytest.mark.asyncio
async def test_invalid_scope_rejected():
    with pytest.raises(ValueError):
        RateLimiter(scope="bogus")
    rl = RateLimiter()
    with pytest.raises(ValueError):
        rl.set_scope("bogus")


@pytest.mark.asyncio
async def test_stats_expose_scope_and_agents():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)
    rl.record_request(agent_id="agent-a")
    rl.record_tokens(1234, agent_id="agent-a")

    stats = rl.stats
    assert stats["scope"] == "per_agent"
    assert stats["agents"]["agent-a"]["rpm_current"] == 1
    assert stats["agents"]["agent-a"]["tpm_current"] == 1234


# --- Fair-share governor tests ---


@pytest.mark.asyncio
async def test_fair_share_squeezes_all_agents_under_contention():
    """Combined traffic over the provider limit shrinks every bucket."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)  # 50 RPM

    for _ in range(40):
        rl.record_request(agent_id="agent-a")
        rl.record_request(agent_id="agent-b")

    # Aggregate 80 > 50 → factor 0.625 → effective limit 31. Each agent is at
    # 40 — fine against its own 50-RPM window, but the governor throttles both.
    assert rl._rpm_wait_seconds(agent_id="agent-a") > 0
    assert rl._rpm_wait_seconds(agent_id="agent-b") > 0
    assert rl.stats["fair_share"]["requests_factor"] == 0.625


@pytest.mark.asyncio
async def test_fair_share_spares_light_agents():
    """The squeeze lands on the heavy agent; a light agent keeps flowing."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(50):
        rl.record_request(agent_id="heavy")
    for _ in range(10):
        rl.record_request(agent_id="light")

    # Aggregate 60 > 50 → factor ≈ 0.833 → effective limit 41:
    # heavy (50) exceeds it; light (10) is nowhere near.
    assert rl._rpm_wait_seconds(agent_id="heavy") > 0
    assert rl._rpm_wait_seconds(agent_id="light") == 0.0


@pytest.mark.asyncio
async def test_fair_share_inactive_under_limit():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(30):
        rl.record_request(agent_id="agent-a")
    for _ in range(15):
        rl.record_request(agent_id="agent-b")

    # Aggregate 45 ≤ 50 → no squeeze at all.
    assert rl._rpm_wait_seconds(agent_id="agent-a") == 0.0
    assert rl._rpm_wait_seconds(agent_id="agent-b") == 0.0
    assert rl.stats["fair_share"]["requests_factor"] == 1.0


@pytest.mark.asyncio
async def test_fair_share_counts_unidentified_traffic():
    """The anonymous/global bucket is part of the aggregate too."""
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(40):
        rl.record_request(agent_id="agent-a")
    for _ in range(40):
        rl.record_request()  # unidentified → global window

    # 80 combined > 50 → effective limit 31 for everyone, including the
    # global bucket view.
    assert rl._rpm_wait_seconds(agent_id="agent-a") > 0
    assert rl._rpm_wait_seconds() > 0


@pytest.mark.asyncio
async def test_fair_share_not_applied_in_global_scope():
    """Global scope needs no governor — the shared window IS the cap."""
    rl = RateLimiter(scope="global")
    rl.configure_from_profile(ANTHROPIC)

    for _ in range(40):
        rl.record_request(agent_id="agent-a")
    assert rl.stats["fair_share"]["requests_factor"] == 1.0


# --- Per-agent limit override tests ---


@pytest.mark.asyncio
async def test_agent_limit_override_throttles_one_agent():
    rl = RateLimiter(agent_limits={"batch-bot": {"rpm": 5}})
    rl.configure_from_profile(ANTHROPIC)  # everyone else: 50 RPM

    for _ in range(5):
        rl.record_request(agent_id="batch-bot")
        rl.record_request(agent_id="interactive")

    assert rl._rpm_wait_seconds(agent_id="batch-bot") > 0
    assert rl._rpm_wait_seconds(agent_id="interactive") == 0.0
    stats = rl.stats
    assert stats["agents"]["batch-bot"]["rpm_limit"] == 5
    assert stats["agents"]["interactive"]["rpm_limit"] == 50
    assert stats["agent_limits"] == {"batch-bot": {"rpm": 5}}


@pytest.mark.asyncio
async def test_set_agent_limits_replaces_registry():
    rl = RateLimiter(agent_limits={"a": {"rpm": 5}})
    rl.set_agent_limits({"b": {"tpm": 1000}})
    # "a" is gone — falls back to the unconfigured default (None, no profile).
    assert rl._limit_for("a", "rpm") is None
    assert rl._limit_for("b", "tpm") == 1000


@pytest.mark.asyncio
async def test_invalid_agent_limits_rejected():
    with pytest.raises(ValueError):
        RateLimiter(agent_limits={"a": {"rpm": 0}})
    with pytest.raises(ValueError):
        RateLimiter(agent_limits={"a": {"qps": 5}})
    with pytest.raises(ValueError):
        RateLimiter(agent_limits={"a": {}})
    with pytest.raises(ValueError):
        RateLimiter(agent_limits=["not-a-dict"])
    rl = RateLimiter()
    with pytest.raises(ValueError):
        rl.set_agent_limits({"a": {"rpm": -3}})


# --- kill-switch: one flag provably disables every throttle path -------------


async def test_disabled_limiter_never_waits_even_when_saturated():
    """Seed limits, force-fill the windows, install a header pause — a
    disabled limiter must still return immediately from wait_if_throttled
    and report not-throttled: the guard lives in _wait_seconds, so no
    call site can bypass it."""
    limiter = RateLimiter(enabled=False)
    limiter._rpm_limit = 1
    limiter._tpm_limit = 10
    # Force-fill the shared windows past their limits (bypassing the
    # disabled record_* guards on purpose — the wait guard must hold
    # regardless of window state).
    now = time.monotonic()
    limiter._request_timestamps.extend([now, now, now, now, now])
    limiter._token_usage.extend([(now, 10_000)] * 5)
    limiter._pause_until = time.time() + 60.0  # header pause active

    assert await limiter.wait_if_throttled() == 0.0
    assert limiter.is_throttled is False
    assert limiter.agent_is_throttled("any-agent") is False
    assert limiter.throttle_remaining_seconds == 0.0


async def test_disabled_limiter_ignores_headers_and_records_nothing():
    limiter = RateLimiter(enabled=False)
    await limiter.update_from_headers(
        {
            "x-ratelimit-remaining-requests": "0",
            "retry-after": "30",
        }
    )
    assert limiter._pause_until == 0.0  # headers could not pause

    limiter.record_request("a")
    limiter.record_tokens(500, agent_id="a")
    assert not limiter._request_timestamps
    assert not limiter._token_usage


def test_disabled_limiter_stats_reports_the_switch():
    limiter = RateLimiter(enabled=False)
    assert limiter.stats["enabled"] is False
    assert RateLimiter().stats["enabled"] is True


def test_every_rate_limiter_construction_passes_enabled():
    """Wiring contract: EVERY RateLimiter(...) call site in the package must
    pass enabled=, so config.rate_limiting_enabled cannot be silently
    dropped by an entrypoint (regression: `hivemind proxy` built its own
    limiter without the kwarg while `serve` had it — the kill-switch was
    dead code on the exact path manifold launches)."""
    import inspect
    import re
    from pathlib import Path

    import hivemind

    pkg = Path(inspect.getfile(hivemind)).parent
    pattern = re.compile(r"RateLimiter\(")
    checked = 0
    for py in pkg.rglob("*.py"):
        src = py.read_text()
        for m in pattern.finditer(src):
            # capture the balanced call up to the closing paren
            depth, i = 1, m.end()
            while depth and i < len(src):
                depth += src[i] == "("
                depth -= src[i] == ")"
                i += 1
            call = src[m.start() : i]
            assert "enabled=" in call, f"{py}:{src[:m.start()].count(chr(10)) + 1} builds RateLimiter without enabled="
            checked += 1
    assert checked >= 2, "expected at least the serve + proxy construction sites"
