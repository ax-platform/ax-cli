#!/usr/bin/env python3
"""Gateway-managed bridge for a LangGraph agent.

This bridge is designed for `ax gateway agents add ... --template langgraph`.
It runs once per inbound mention: read the prompt, route it through a
LangGraph StateGraph, and print the reply on stdout.

The initial cut intentionally ships with a stub graph. The point of
this slice is the Gateway-side plumbing: prove the runtime registers,
emits AX_GATEWAY_EVENT lifecycle signals (processing -> completed),
and rounds a reply through the Gateway end to end. Real LangGraph
orchestration with multi-node graphs, tool calls, and streaming events
is a follow-up.

If `langgraph` is importable, the bridge runs a one-node StateGraph so
the wiring is exercised. If not, it falls back to a string template.
Either path emits the same lifecycle events.
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
        or "langgraph-bot"
    )


def _run_stub_graph(prompt: str) -> str:
    """Run a one-node LangGraph if available, else a string template.

    The graph is intentionally trivial. The point of this slice is to
    prove the Gateway-side adapter, not the orchestration. Future
    iterations will introduce real multi-node graphs with tool-call
    telemetry mapped to Gateway tool bubbles.
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        emit_event(
            {
                "kind": "activity",
                "activity": "langgraph not installed; using stub reply (install langgraph for real graph execution)",
            }
        )
        return f"LangGraph stub ack from @{_agent_name()}: {prompt}"

    emit_event({"kind": "activity", "activity": "building one-node StateGraph"})

    def _ack_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"reply": f"LangGraph ack from @{_agent_name()}: {state.get('prompt', '')}"}

    graph = StateGraph(dict)
    graph.add_node("ack", _ack_node)
    graph.add_edge(START, "ack")
    graph.add_edge("ack", END)
    app = graph.compile()

    result = app.invoke({"prompt": prompt})
    return str(result.get("reply") or "")


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
            "message": "Routing prompt through LangGraph bridge",
        }
    )

    try:
        reply = _run_stub_graph(prompt)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"LangGraph bridge failed: {exc}", file=sys.stderr)
        return 1

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"LangGraph bridge completed in {duration_ms}ms",
            "detail": {"duration_ms": duration_ms, "stub": True},
        }
    )
    print(reply or f"LangGraph bridge for @{_agent_name()} finished without text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
