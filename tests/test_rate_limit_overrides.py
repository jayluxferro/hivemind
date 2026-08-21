"""Override test for the rate limiter — CLI/config limits on top of profiles."""

import pytest

from hivemind.scheduler.rate_limiter import RateLimiter
from hivemind.scheduler.providers import ANTHROPIC


@pytest.mark.asyncio
async def test_apply_overrides_replaces_only_given_limits():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)
    assert rl._rpm_limit == ANTHROPIC.default_requests_per_minute
    assert rl._tpm_limit == ANTHROPIC.default_tokens_per_minute

    # Only TPM overridden — RPM keeps the profile default.
    rl.apply_overrides(tpm=250_000)
    assert rl._rpm_limit == ANTHROPIC.default_requests_per_minute
    assert rl._tpm_limit == 250_000

    # RPM override stacks on top without disturbing TPM.
    rl.apply_overrides(rpm=100)
    assert rl._rpm_limit == 100
    assert rl._tpm_limit == 250_000

    # No-op override changes nothing.
    before = (rl._rpm_limit, rl._tpm_limit)
    rl.apply_overrides()
    assert (rl._rpm_limit, rl._tpm_limit) == before


@pytest.mark.asyncio
async def test_overridden_tpm_governs_throttling():
    rl = RateLimiter()
    rl.configure_from_profile(ANTHROPIC)
    rl.apply_overrides(tpm=1_000_000)

    # A request triple the profile TPM fits inside the override.
    import time

    now = time.monotonic()
    rl._token_usage.append((now, 3 * ANTHROPIC.default_tokens_per_minute))
    assert not rl.is_throttled
