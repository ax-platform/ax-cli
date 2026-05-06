#!/usr/bin/env python3
"""Gateway-managed bridge for a Strands agent.

This bridge is designed for `ax gateway agents add ... --template strands`.
It runs once per inbound mention: read the prompt, route it through a
Strands agent, and print the reply on stdout.

The initial cut intentionally ships with a stub. The point of this slice
is the Gateway-side plumbing: prove the runtime registers, emits
AX_GATEWAY_EVENT lifecycle signals (processing -> completed), and rounds
a reply through the Gateway end to end. Real Strands agent execution
(model-backed Agent.invoke, tool calls mapped to Gateway tool bubbles,
streaming events) is a follow-up.

If `strands` is importable, the bridge logs that and still returns the
stub reply. A real Agent run requires an LLM endpoint + credentials and
is intentionally out of scope for this slice. If `strands` is not
importable, the bridge falls back to the same string template. Either
path emits the same lifecycle events.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

EVENT_PREFIX = "AX_GATEWAY_EVENT "


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _read_prompt() -> str:
    if len(sys.argv) > 1 and sys.argv[-1] != "-":
        return sys.argv[-1]
    env_prompt = os.environ.get("AX_MENTION_CONTENT", "").strip()
    if env_prompt:
        return env_prompt
    return sys.stdin.read().strip()


def _agent_name() -> str:
    return (
        os.environ.get("AX_GATEWAY_AGENT_NAME", "").strip()
        or os.environ.get("AX_AGENT_NAME", "").strip()
        or "strands-bot"
    )


def _run_stub_agent(prompt: str) -> str:
    """Return a stub reply, optionally noting whether strands is installed.

    The reply shape is intentionally trivial. The point of this slice is
    to prove the Gateway-side adapter, not the orchestration. Future
    iterations will instantiate a real Strands Agent, map tool calls to
    Gateway tool bubbles, and stream model output as activity events.
    """
    try:
        import strands  # noqa: F401
    except ImportError:
        emit_event(
            {
                "kind": "activity",
                "activity": "strands not installed; using stub reply (install strands for real agent execution)",
            }
        )
        return f"Strands stub ack from @{_agent_name()}: {prompt}"

    emit_event(
        {
            "kind": "activity",
            "activity": "strands module loaded; stub reply only (real Agent execution is a follow-up)",
        }
    )
    return f"Strands ack from @{_agent_name()}: {prompt}"


def main() -> int:
    prompt = _read_prompt()
    if not prompt:
        print("(no mention content received)", file=sys.stderr)
        return 1

    started = time.monotonic()
    emit_event(
        {
            "kind": "status",
            "status": "processing",
            "message": "Routing prompt through Strands bridge",
        }
    )

    try:
        reply = _run_stub_agent(prompt)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"Strands bridge failed: {exc}", file=sys.stderr)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"Strands bridge completed in {duration_ms}ms",
            "detail": {"duration_ms": duration_ms, "stub": True},
        }
    )
    print(reply or f"Strands bridge for @{_agent_name()} finished without text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
