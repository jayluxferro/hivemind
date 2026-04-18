"""Tests for token counting from API payloads."""

import json

from hivemind.proxy.token_counter import (
    count_request_tokens,
    count_response_tokens,
    count_streaming_tokens,
    estimate_tokens,
)


def test_estimate_tokens():
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 1  # min 1


def test_count_request_tokens_simple():
    body = json.dumps({
        "messages": [
            {"role": "user", "content": "Hello, how are you?"}
        ]
    }).encode()
    tokens = count_request_tokens(body)
    assert tokens > 0


def test_count_request_tokens_with_system():
    body = json.dumps({
        "system": "You are a helpful assistant.",
        "messages": [
            {"role": "user", "content": "Hi"}
        ]
    }).encode()
    tokens = count_request_tokens(body)
    assert tokens > 0


def test_count_request_tokens_with_tools():
    body = json.dumps({
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": [
            {
                "name": "search",
                "description": "Search the web",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            }
        ],
    }).encode()
    tokens = count_request_tokens(body)
    assert tokens > 5  # Should count tool definitions


def test_count_response_tokens_anthropic():
    body = json.dumps({
        "content": [{"type": "text", "text": "Hello!"}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }).encode()
    input_t, output_t = count_response_tokens(body)
    assert input_t == 100
    assert output_t == 50


def test_count_response_tokens_openai():
    body = json.dumps({
        "choices": [{"message": {"content": "Hello!"}}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 30},
    }).encode()
    input_t, output_t = count_response_tokens(body)
    assert input_t == 80
    assert output_t == 30


def test_count_response_tokens_no_usage():
    body = json.dumps({
        "content": [{"type": "text", "text": "Some response text here"}],
    }).encode()
    input_t, output_t = count_response_tokens(body)
    assert input_t == 0
    assert output_t > 0  # Should estimate from content


def test_count_streaming_tokens_usage_chunk():
    chunk = b'data: {"type": "message_delta", "usage": {"input_tokens": 50, "output_tokens": 20}}\n\n'
    input_t, output_t = count_streaming_tokens(chunk)
    assert input_t == 50
    assert output_t == 20


def test_count_streaming_tokens_no_usage():
    chunk = b'data: {"type": "content_block_delta", "delta": {"text": "Hello"}}\n\n'
    input_t, output_t = count_streaming_tokens(chunk)
    assert input_t == 0
    assert output_t == 0


def test_count_streaming_tokens_done():
    chunk = b"data: [DONE]\n\n"
    input_t, output_t = count_streaming_tokens(chunk)
    assert input_t == 0
    assert output_t == 0


def test_invalid_json():
    body = b"not json"
    tokens = count_request_tokens(body)
    assert tokens > 0  # Falls back to estimation

    input_t, output_t = count_response_tokens(body)
    assert output_t > 0
