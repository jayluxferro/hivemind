"""Tests for passive prompt-cache telemetry."""

import json

from hivemind.scheduler.cache_telemetry import (
    CacheTelemetry,
    extract_cache_usage,
    request_uses_cache_control,
)


def _anthropic_response(read_tokens: int = 0, creation_tokens: int = 0) -> bytes:
    return json.dumps(
        {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 3,
                "cache_read_input_tokens": read_tokens,
                "cache_creation_input_tokens": creation_tokens,
            },
        }
    ).encode()


def _openai_response(cached: int = 0) -> bytes:
    return json.dumps(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": cached},
            },
        }
    ).encode()


def test_extract_cache_usage_anthropic_style() -> None:
    fields = extract_cache_usage(_anthropic_response(read_tokens=900, creation_tokens=300))
    assert fields == {"cache_read_input_tokens": 900, "cache_creation_input_tokens": 300}


def test_extract_cache_usage_openai_style() -> None:
    assert extract_cache_usage(_openai_response(cached=750)) == {"cached_tokens": 750}


def test_extract_cache_usage_absent() -> None:
    assert extract_cache_usage(_anthropic_response()) == {}
    assert extract_cache_usage(b"not json") == {}


def test_extract_cache_usage_nested_streaming_shape() -> None:
    # message_start SSE events nest usage under "message".
    event = json.dumps(
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 12,
                    "cache_read_input_tokens": 3008,
                    "cache_creation_input_tokens": 0,
                }
            },
        }
    ).encode()
    assert extract_cache_usage(event) == {"cache_read_input_tokens": 3008}


def test_request_marker_detection() -> None:
    body = json.dumps({"system": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}).encode()
    assert request_uses_cache_control(body)
    assert not request_uses_cache_control(json.dumps({"messages": []}).encode())


def test_telemetry_accumulates_and_flips_supported() -> None:
    t = CacheTelemetry()
    assert not t.cache_supported

    t.observe_request(b'{"messages": [{"content": "hi"}]}')
    t.observe_request(b'{"system": [{"cache_control": {"type": "ephemeral"}}]}')
    t.observe_response(_anthropic_response(creation_tokens=1100))
    t.observe_response(_anthropic_response(read_tokens=1100))
    t.observe_response(b'{"usage": {"input_tokens": 5}}')  # no cache fields

    snap = t.snapshot()
    assert snap["requests_seen"] == 2
    assert snap["requests_with_cache_control"] == 1
    assert snap["responses_with_cache_usage"] == 2
    assert snap["cache_creation_tokens"] == 1100
    assert snap["cache_read_tokens"] == 1100
    assert t.cache_supported


def test_record_probe_verdict() -> None:
    t = CacheTelemetry()
    assert not t.record_probe({"input_tokens": 1200})
    assert not t.cache_supported
    assert t.record_probe({"cache_read_input_tokens": 1100})
    assert t.cache_supported
    assert t.snapshot()["cache_read_tokens"] == 1100
