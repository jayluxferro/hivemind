"""Backward-compatible re-exports; prefer `hivemind.cli_args`."""

from __future__ import annotations

from ..cli_args import hivemind_config_from_proxy_cli_args, register_proxy_cli_arguments

__all__ = [
    "hivemind_config_from_proxy_cli_args",
    "register_proxy_cli_arguments",
]
