"""Tests for streaming SSE pass-through."""

import json
import pytest

from hivemind.proxy.streaming import is_streaming_request, parse_sse_chunk


def test_detect_streaming_from_body():
    headers = {"content-type": "application/json"}
    body = json.dumps({"stream": True, "messages": []}).encode()
    assert is_streaming_request(headers, body) is True


def test_detect_non_streaming():
    headers = {"content-type": "application/json"}
    body = json.dumps({"stream": False, "messages": []}).encode()
    assert is_streaming_request(headers, body) is False


def test_detect_streaming_no_stream_field():
    headers = {"content-type": "application/json"}
    body = json.dumps({"messages": []}).encode()
    assert is_streaming_request(headers, body) is False


def test_detect_streaming_from_accept_header():
    headers = {"accept": "text/event-stream"}
    body = b"{}"
    assert is_streaming_request(headers, body) is True


def test_parse_sse_content_block():
    chunk = b'event: content_block_delta\ndata: {"type": "content_block_delta", "delta": {"text": "Hello"}}\n\n'
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert tokens_in == 0
    assert tokens_out == 0
    assert is_final is False


def test_parse_sse_message_start_with_usage():
    chunk = b'event: message_start\ndata: {"type": "message_start", "message": {"usage": {"input_tokens": 100}}}\n\n'
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert tokens_in == 100
    assert tokens_out == 0
    assert is_final is False


def test_parse_sse_message_delta_with_usage():
    chunk = b'event: message_delta\ndata: {"type": "message_delta", "usage": {"output_tokens": 250}}\n\n'
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert tokens_in == 0
    assert tokens_out == 250
    assert is_final is False


def test_parse_sse_message_stop():
    chunk = b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert tokens_in == 0
    assert tokens_out == 0
    assert is_final is True


def test_parse_sse_done():
    chunk = b"data: [DONE]\n\n"
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert is_final is True


def test_parse_sse_openai_finish():
    chunk = b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert is_final is True


def test_parse_sse_openai_usage():
    chunk = b'data: {"usage": {"prompt_tokens": 50, "completion_tokens": 120}}\n\n'
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert tokens_in == 50
    assert tokens_out == 120


def test_parse_sse_invalid():
    chunk = b"not valid sse"
    tokens_in, tokens_out, is_final = parse_sse_chunk(chunk)
    assert tokens_in == 0
    assert tokens_out == 0
    assert is_final is False
