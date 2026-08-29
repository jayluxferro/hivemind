"""Tests for proxy agent-identity resolution."""

import json

from hivemind.proxy.identity import (
    ANONYMOUS_AGENT_ID,
    resolve_agent_id,
    resolve_rate_key,
    resolve_session_hint,
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


# --- Session hint from request-body metadata.user_id ---


def _body(user_id) -> bytes:
    return json.dumps({"metadata": {"user_id": user_id}, "messages": []}).encode()


def test_metadata_user_id_separates_same_key_sessions():
    """Two same-key sessions of one tool get distinct buckets via the body hint."""
    headers = {"authorization": "Bearer sk-ant-shared", "user-agent": "claude-cli/1.0"}
    s1 = _body("user_abc_account__session_1111")
    s2 = _body("user_abc_account__session_2222")

    k1 = resolve_rate_key(headers, {}, s1)
    k2 = resolve_rate_key(headers, {}, s2)
    assert k1 != k2
    assert k1.startswith("mu-") and k2.startswith("mu-")
    assert k1 == resolve_rate_key(headers, {}, s1)  # stable per session


def test_session_hint_namespaced_by_credential():
    body = _body("user_abc_account__session_1111")
    a = resolve_rate_key({"authorization": "Bearer sk-1"}, {}, body)
    b = resolve_rate_key({"authorization": "Bearer sk-2"}, {}, body)
    assert a != b


def test_session_hint_beats_fingerprint_but_not_explicit():
    headers = {"authorization": "Bearer sk-1", "user-agent": "tool/1"}
    body = _body("user_x_account__session_y")

    assert resolve_rate_key(headers, {}, body).startswith("mu-")
    assert resolve_rate_key(headers, {}).startswith("fp-")  # no body → fingerprint
    explicit = {"x-hivemind-agent-id": "me", **headers}
    assert resolve_rate_key(explicit, {}, body) == "me"  # explicit always wins


def test_session_hint_ignores_malformed_bodies():
    headers = {"authorization": "Bearer sk-1", "user-agent": "tool/1"}
    for bad in (b"not json", b"[1,2]", b'{"metadata": "nope"}', b'{"metadata": {"user_id": 42}}', b""):
        assert resolve_rate_key(headers, {}, bad).startswith("fp-")


def test_session_hint_never_contains_raw_value():
    user_id = "user_secretvalue_account__session_leak"
    key = resolve_rate_key({"authorization": "Bearer sk-1"}, {}, _body(user_id))
    assert "leak" not in key
    assert "secretvalue" not in key


def test_resolve_session_hint_direct():
    assert resolve_session_hint(_body("  user_a_account__session_b  ")) == "user_a_account__session_b"
    assert resolve_session_hint(None) is None
    assert resolve_session_hint(b"") is None
    assert resolve_session_hint(b"not json") is None
