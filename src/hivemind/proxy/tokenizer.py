"""Model-specific tokenizer — accurate token counting when libraries are available.

Priority:
1. anthropic tokenizer (for Anthropic models)
2. tiktoken (for OpenAI models)
3. Heuristic fallback (4 chars per token)

All tokenizer deps are optional — the system works without them.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# Heuristic fallback
CHARS_PER_TOKEN = 4

_anthropic_tokenizer = None
_tiktoken_available = False
_anthropic_available = False


def _init_anthropic():
    global _anthropic_tokenizer, _anthropic_available
    try:
        from anthropic import Anthropic

        _anthropic_tokenizer = Anthropic()
        _anthropic_available = True
        logger.debug("Tokenizer: anthropic library available")
    except (ImportError, Exception):
        _anthropic_available = False


def _init_tiktoken():
    global _tiktoken_available
    try:
        import tiktoken  # noqa: F401

        _tiktoken_available = True
        logger.debug("Tokenizer: tiktoken available")
    except ImportError:
        _tiktoken_available = False


# Initialize on first import
_init_anthropic()
_init_tiktoken()


@lru_cache(maxsize=16)
def _get_tiktoken_encoding(model: str):
    """Get tiktoken encoding for a model (cached)."""
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens_for_model(text: str, model: str = "") -> int:
    """Count tokens using the best available tokenizer for the model.

    Args:
        text: The text to tokenize
        model: Model name (e.g., "claude-sonnet-4-20250514", "gpt-4o")

    Returns:
        Token count
    """
    if not text:
        return 0

    model_lower = model.lower()

    # Try anthropic tokenizer for Claude models
    if _anthropic_available and ("claude" in model_lower or "anthropic" in model_lower or not model):
        try:
            result = _anthropic_tokenizer.count_tokens(text)
            return result
        except Exception:
            pass

    # Try tiktoken for OpenAI models
    if _tiktoken_available and ("gpt" in model_lower or "o1" in model_lower or "o3" in model_lower):
        try:
            enc = _get_tiktoken_encoding(model)
            return len(enc.encode(text))
        except Exception:
            pass

    # Also try tiktoken as generic fallback (good for most models)
    if _tiktoken_available:
        try:
            enc = _get_tiktoken_encoding("gpt-4")  # cl100k_base — reasonable default
            return len(enc.encode(text))
        except Exception:
            pass

    # Heuristic fallback
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_messages_tokens(messages: list[dict], model: str = "") -> int:
    """Count tokens for a list of chat messages."""
    total = 0
    for msg in messages:
        # Role token
        total += 4  # <role> overhead per message
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens_for_model(content, model)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if "text" in block:
                        total += count_tokens_for_model(block["text"], model)
                    elif "source" in block:
                        total += 1000  # Image estimate
    return total


def get_tokenizer_info() -> dict:
    """Report which tokenizers are available."""
    return {
        "anthropic_available": _anthropic_available,
        "tiktoken_available": _tiktoken_available,
        "method": ("anthropic" if _anthropic_available else "tiktoken" if _tiktoken_available else "heuristic"),
    }
