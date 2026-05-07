"""Regression: --verbose must accompany --print + --output-format stream-json
in every Claude Code subprocess command we build.

Without --verbose the Claude CLI rejects the combination on Mac/Linux:
    "When using --print, --output-format=stream-json requires --verbose"

The fix in ax_cli/gateway.py::_build_sentinel_claude_cmd was already landed
(see the cmd list there). This module locks the same behavior on the two
remaining call sites that vendor the Hermes runtime.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from ax_cli.runtimes.hermes.runtimes.claude_cli import ClaudeCLIRuntime
from ax_cli.runtimes.hermes.sentinel import _build_claude_cmd


def test_hermes_sentinel_build_claude_cmd_includes_verbose() -> None:
    args = SimpleNamespace(model=None, allowed_tools=None, system_prompt=None)
    cmd = _build_claude_cmd("hello", "/tmp/wd", args)
    assert "-p" in cmd, f"expected --print (-p) in cmd: {cmd}"
    assert "stream-json" in cmd, f"expected stream-json in cmd: {cmd}"
    assert "--verbose" in cmd, (
        f"--verbose missing from Claude sentinel cmd; Claude CLI requires "
        f"it whenever --print + --output-format stream-json are set. cmd={cmd}"
    )


def test_claude_cli_runtime_execute_constructs_verbose_cmd() -> None:
    src = inspect.getsource(ClaudeCLIRuntime.execute)
    assert '"--verbose"' in src or "'--verbose'" in src, (
        "ClaudeCLIRuntime.execute() builds a Claude cmd missing --verbose. "
        "Claude CLI rejects --print + --output-format stream-json without it."
    )
