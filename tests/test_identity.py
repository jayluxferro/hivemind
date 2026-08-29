"""Tests for proxy agent-identity resolution."""

from hivemind.proxy.identity import (
    ANONYMOUS_AGENT_ID,
    resolve_agent_id,
    resolve_rate_key,
)


def test_explicit_agent_id_header_wins():
    headers = {"x-hivemind-agent-id": "session-1", "authorization": "Bearer sk-secret"}
    assert resolve_agent_id(headers, {}) == "session-1"
    assert resolve_rate_key(headers, {}) == "session-1"


def test_session_id_header_accepted():
    assert resolve_agent_id({"x-hivemind-session-id": "s-2"}, {}) == "s-2"


def test_query_param_fallback():
    assert resolve_agent_id({}, {"agent_id": "s-3"}) == "s-3"


def test_header_beats_query_param():
    assert resolve_agent_id({"x-hivemind-agent-id": "h"}, {"agent_id": "q"}) == "h"


def test_no_identity_returns_none():
    """Budgets/logging keep their untracked behavior for unidentified traffic."""
    assert resolve_agent_id({}, {}) is None


def test_fingerprint_stable_for_same_credential():
    headers = {"authorization": "Bearer sk-ant-abc", "user-agent": "claude-cli/1.0"}
    assert resolve_rate_key(headers, {}) == resolve_rate_key(dict(headers), {})


def test_fingerprint_distinguishes_credentials():
    a = {"authorization": "Bearer sk-1", "user-agent": "tool/1"}
    b = {"authorization": "Bearer sk-2", "user-agent": "tool/1"}
    assert resolve_rate_key(a, {}) != resolve_rate_key(b, {})


def test_fingerprint_distinguishes_user_agents():
    a = {"authorization": "Bearer sk-1", "user-agent": "claude-cli/1.0"}
    b = {"authorization": "Bearer sk-1", "user-agent": "cursor/2.0"}
    assert resolve_rate_key(a, {}) != resolve_rate_key(b, {})


def test_fingerprint_never_contains_raw_credential():
    key = "sk-ant-super-secret-key"
    rate_key = resolve_rate_key({"authorization": f"Bearer {key}"}, {})
    assert key not in rate_key
    assert rate_key.startswith("fp-")


def test_x_api_key_used_as_credential():
    assert resolve_rate_key({"x-api-key": "sk-secret"}, {}).startswith("fp-")


def test_user_agent_only_fingerprint():
    assert resolve_rate_key({"user-agent": "my-tool/3"}, {}).startswith("ua-")


def test_anonymous_last_resort():
    assert resolve_rate_key({}, {}) == ANONYMOUS_AGENT_ID


def test_blank_identity_treated_as_missing():
    assert resolve_agent_id({"x-hivemind-agent-id": "   "}, {}) is None


def test_identity_length_capped():
    """A hostile client can't grow the limiter's agent dict with huge keys."""
    assert len(resolve_rate_key({"x-hivemind-agent-id": "x" * 500}, {})) == 128
