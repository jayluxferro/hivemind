"""Tests for model-specific tokenizer."""

from hivemind.proxy.tokenizer import (
    count_messages_tokens,
    count_tokens_for_model,
    get_tokenizer_info,
)


def test_heuristic_fallback():
    # Should always work regardless of installed libraries
    count = count_tokens_for_model("Hello, world!", model="unknown-model-xyz")
    assert count > 0


def test_empty_string():
    assert count_tokens_for_model("") == 0


def test_long_text():
    text = "word " * 1000
    count = count_tokens_for_model(text)
    # Should be roughly 1000 tokens (each "word " is ~1-2 tokens)
    assert 500 < count < 2000


def test_count_messages():
    messages = [
        {"role": "user", "content": "Hello, how are you?"},
        {"role": "assistant", "content": "I'm doing well, thank you!"},
    ]
    count = count_messages_tokens(messages)
    assert count > 0


def test_count_messages_with_blocks():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image", "source": {"type": "base64", "data": "..."}},
            ],
        }
    ]
    count = count_messages_tokens(messages)
    assert count >= 1000  # Image block adds 1000


def test_tokenizer_info():
    info = get_tokenizer_info()
    assert "method" in info
    assert info["method"] in ("anthropic", "tiktoken", "heuristic")
    assert "anthropic_available" in info
    assert "tiktoken_available" in info
