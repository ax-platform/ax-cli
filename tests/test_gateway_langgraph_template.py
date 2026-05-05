"""Regression: LangGraph template registers correctly and the bridge
emits the Gateway lifecycle contract.

A Gateway-managed LangGraph runtime should be declarable from the
dashboard / CLI like any other template, and the bridge subprocess
should emit AX_GATEWAY_EVENT lines that map to the three signals
operators rely on (online via heartbeat, accept-work via intake_model,
response-path via return_paths).

This file locks the initial contract:
  - the template appears in agent_template_catalog with the right shape
  - the template appears in the default agent_template_list ordering
  - the bridge file exists at the path the template advertises
  - the bridge emits a "processing" event and a "completed" event around
    a stub prompt round trip
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from ax_cli.gateway_runtime_types import (
    agent_template_catalog,
    agent_template_definition,
    agent_template_list,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO_ROOT / "examples" / "gateway_langgraph" / "langgraph_bridge.py"


def test_langgraph_template_is_registered() -> None:
    catalog = agent_template_catalog()
    assert "langgraph" in catalog, (
        "langgraph template missing from agent_template_catalog. "
        "Should sit alongside ollama / hermes / claude_code_channel."
    )

    template = agent_template_definition("langgraph")
    assert template["id"] == "langgraph"
    assert template["runtime_type"] == "exec", (
        "Reuses the exec runtime adapter (same precedent as ollama). "
        "A dedicated 'langgraph' runtime_type is a follow-up."
    )
    assert template["intake_model"] == "launch_on_send"
    assert template["return_paths"] == ["inline_reply"]
    assert template["availability"] == "ready"
    assert template["launchable"] is True


def test_langgraph_template_default_exec_command_points_at_bridge() -> None:
    template = agent_template_definition("langgraph")
    defaults = template.get("defaults") or {}
    exec_command = str(defaults.get("exec_command") or "")
    assert "examples/gateway_langgraph/langgraph_bridge.py" in exec_command, (
        f"langgraph template's default exec_command should run the stub "
        f"bridge at examples/gateway_langgraph/langgraph_bridge.py. Got: {exec_command!r}"
    )


def test_langgraph_template_listed_in_default_ordering() -> None:
    listed_ids = [item["id"] for item in agent_template_list()]
    assert "langgraph" in listed_ids, (
        "langgraph template should appear in the default (non-advanced) "
        "template list so the dashboard's Add Agent modal can render it."
    )


def test_langgraph_bridge_file_exists() -> None:
    assert BRIDGE_PATH.exists(), (
        f"langgraph bridge file missing at {BRIDGE_PATH}. The default "
        "exec_command in the template registration points at it."
    )


def test_langgraph_bridge_emits_lifecycle_events(monkeypatch, capsys) -> None:
    """Run the bridge's main() inline and confirm it emits processing
    and completed AX_GATEWAY_EVENT lines around the stub round trip."""
    sys.path.insert(0, str(BRIDGE_PATH.parent))
    try:
        import langgraph_bridge as bridge
    finally:
        sys.path.pop(0)

    monkeypatch.setattr(sys, "argv", ["langgraph_bridge.py", "test prompt"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setenv("AX_GATEWAY_AGENT_NAME", "langgraph-test")

    rc = bridge.main()
    captured = capsys.readouterr()

    assert rc == 0, f"bridge main() returned {rc}; expected 0"

    event_lines = [line for line in captured.out.splitlines() if line.startswith(bridge.EVENT_PREFIX)]
    statuses = []
    for line in event_lines:
        payload = json.loads(line[len(bridge.EVENT_PREFIX) :])
        if payload.get("kind") == "status":
            statuses.append(payload.get("status"))

    assert "processing" in statuses, f"bridge did not emit a processing status event. statuses={statuses}"
    assert "completed" in statuses, f"bridge did not emit a completed status event. statuses={statuses}"

    reply_lines = [line for line in captured.out.splitlines() if line and not line.startswith(bridge.EVENT_PREFIX)]
    assert reply_lines, "bridge did not print a reply line on stdout"
    assert "test prompt" in reply_lines[-1], (
        f"bridge reply should echo the prompt in the stub. last line: {reply_lines[-1]!r}"
    )
