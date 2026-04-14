"""Tests for ax handoff composed workflow helpers."""

import json

from typer.testing import CliRunner

from ax_cli.commands.handoff import (
    _completion_promise_satisfied,
    _is_handoff_progress,
    _matches_handoff_progress,
    _matches_handoff_reply,
)
from ax_cli.main import app


def _json_tail(output: str) -> dict:
    start = output.rfind("\n{")
    if start == -1:
        start = output.find("{")
    else:
        start += 1
    return json.loads(output[start:])


def test_handoff_matches_thread_reply_from_target_agent():
    message = {
        "id": "reply-1",
        "content": "Reviewed and done.",
        "parent_id": "sent-1",
        "display_name": "orion",
        "created_at": "2026-04-13T04:31:00+00:00",
    }

    assert _matches_handoff_reply(
        message,
        agent_name="orion",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )


def test_handoff_matches_fast_top_level_reply_with_token_and_mention():
    message = {
        "id": "reply-1",
        "content": "@ChatGPT handoff:abc123 reviewed the spec.",
        "conversation_id": "reply-1",
        "display_name": "orion",
        "created_at": "2026-04-13T04:31:00+00:00",
    }

    assert _matches_handoff_reply(
        message,
        agent_name="@orion",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=True,
    )


def test_handoff_does_not_match_other_agent():
    message = {
        "id": "reply-1",
        "content": "@ChatGPT handoff:abc123 done.",
        "display_name": "cipher",
    }

    assert not _matches_handoff_reply(
        message,
        agent_name="orion",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )


def test_handoff_progress_does_not_count_as_reply():
    message = {
        "id": "reply-1",
        "content": "Working... (12 tools)\n  > checking repo\n  > running tests",
        "parent_id": "sent-1",
        "display_name": "mcp_sentinel",
        "metadata": {"streaming_reply": {"enabled": True, "final": False}},
    }

    assert _is_handoff_progress(message)
    assert _matches_handoff_progress(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )
    assert not _matches_handoff_reply(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )


def test_handoff_progress_can_change_without_matching_completion():
    message = {
        "id": "reply-2",
        "content": "Working... (41 tools)\n  > ax context load\n  > ax messages list",
        "conversation_id": "sent-1",
        "display_name": "mcp_sentinel",
    }

    assert _is_handoff_progress(message)
    assert _matches_handoff_progress(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=False,
    )
    assert not _matches_handoff_reply(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=True,
    )


def test_handoff_streaming_reply_with_token_counts_as_reply():
    message = {
        "id": "reply-3",
        "content": "Received this. `handoff:abc123` Current state: smoke check acknowledged.",
        "parent_id": "sent-1",
        "display_name": "mcp_sentinel",
        "metadata": {"streaming_reply": {"enabled": True, "final": False}},
    }

    assert _is_handoff_progress(message)
    assert _matches_handoff_reply(
        message,
        agent_name="mcp_sentinel",
        sent_message_id="sent-1",
        token="handoff:abc123",
        current_agent_name="ChatGPT",
        started_at=0,
        require_completion=True,
    )


def test_completion_promise_requires_exact_tag_or_line():
    assert _completion_promise_satisfied("<promise>DONE</promise>", "DONE")
    assert _completion_promise_satisfied("DONE\n", "DONE")
    assert not _completion_promise_satisfied("not DONE yet", "DONE")
    assert not _completion_promise_satisfied("<promise>DONE-ish</promise>", "DONE")


def test_handoff_loop_repeats_until_completion_promise(monkeypatch):
    runner = CliRunner()
    calls = {"messages": []}

    class FakeClient:
        def list_agents(self):
            return {"agents": [{"id": "agent-1", "name": "orion"}]}

        def create_task(self, space_id, title, description=None, priority=None, assignee_id=None):
            calls["task"] = {
                "space_id": space_id,
                "title": title,
                "description": description,
                "priority": priority,
                "assignee_id": assignee_id,
            }
            return {"task": {"id": "task-1"}}

        def send_message(self, space_id, content, parent_id=None):
            message_id = f"msg-{len(calls['messages']) + 1}"
            calls["messages"].append({"space_id": space_id, "content": content, "parent_id": parent_id})
            return {"message": {"id": message_id}}

    replies = [
        {"id": "reply-1", "content": "handoff:abc still working", "display_name": "orion"},
        {"id": "reply-2", "content": "<promise>DONE</promise>", "display_name": "orion"},
    ]

    monkeypatch.setattr("ax_cli.commands.handoff.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.handoff.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.handoff.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.handoff.uuid.uuid4", lambda: type("UUID", (), {"hex": "abc123456789"})())
    monkeypatch.setattr("ax_cli.commands.handoff._wait_for_handoff_reply", lambda *args, **kwargs: replies.pop(0))

    result = runner.invoke(
        app,
        [
            "handoff",
            "orion",
            "Fix the regression with tests",
            "--loop",
            "--max-rounds",
            "3",
            "--completion-promise",
            "DONE",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    data = _json_tail(result.output)
    assert len(calls["messages"]) == 2
    assert "Agentic loop mode is enabled" in calls["messages"][0]["content"]
    assert calls["messages"][1]["content"].startswith("@orion Continue agentic loop")
    assert calls["messages"][1]["parent_id"] == "reply-1"
    assert data["reply"]["id"] == "reply-2"
    assert data["loop"]["completed"] is True
    assert data["loop"]["stop_reason"] == "completion_promise"
    assert len(data["loop"]["rounds"]) == 2


def test_handoff_loop_stops_at_max_rounds_without_promise(monkeypatch):
    runner = CliRunner()
    calls = {"messages": []}

    class FakeClient:
        def list_agents(self):
            return {"agents": [{"id": "agent-1", "name": "orion"}]}

        def create_task(self, space_id, title, description=None, priority=None, assignee_id=None):
            return {"task": {"id": "task-1"}}

        def send_message(self, space_id, content, parent_id=None):
            message_id = f"msg-{len(calls['messages']) + 1}"
            calls["messages"].append({"space_id": space_id, "content": content, "parent_id": parent_id})
            return {"message": {"id": message_id}}

    replies = [
        {"id": "reply-1", "content": "round one", "display_name": "orion"},
        {"id": "reply-2", "content": "round two", "display_name": "orion"},
    ]

    monkeypatch.setattr("ax_cli.commands.handoff.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.handoff.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.handoff.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.handoff._wait_for_handoff_reply", lambda *args, **kwargs: replies.pop(0))

    result = runner.invoke(app, ["handoff", "orion", "Iterate twice", "--loop", "--max-rounds", "2", "--json"])

    assert result.exit_code == 0, result.output
    data = _json_tail(result.output)
    assert len(calls["messages"]) == 2
    assert data["reply"]["id"] == "reply-2"
    assert data["loop"]["completed"] is False
    assert data["loop"]["stop_reason"] == "max_rounds"
    assert len(data["loop"]["rounds"]) == 2


def test_handoff_loop_timeout_after_progress_uses_loop_timeout_status(monkeypatch):
    runner = CliRunner()
    calls = {"messages": []}

    class FakeClient:
        def list_agents(self):
            return {"agents": [{"id": "agent-1", "name": "orion"}]}

        def create_task(self, space_id, title, description=None, priority=None, assignee_id=None):
            return {"task": {"id": "task-1"}}

        def send_message(self, space_id, content, parent_id=None):
            message_id = f"msg-{len(calls['messages']) + 1}"
            calls["messages"].append({"space_id": space_id, "content": content, "parent_id": parent_id})
            return {"message": {"id": message_id}}

    replies = [{"id": "reply-1", "content": "round one", "display_name": "orion"}, None]

    monkeypatch.setattr("ax_cli.commands.handoff.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.handoff.resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr("ax_cli.commands.handoff.resolve_agent_name", lambda client=None: "ChatGPT")
    monkeypatch.setattr("ax_cli.commands.handoff._wait_for_handoff_reply", lambda *args, **kwargs: replies.pop(0))

    result = runner.invoke(app, ["handoff", "orion", "Iterate twice", "--loop", "--max-rounds", "2", "--json"])

    assert result.exit_code == 0, result.output
    data = _json_tail(result.output)
    assert data["status"] == "loop_timeout"
    assert data["reply"] is None
    assert data["loop"]["stop_reason"] == "timeout"
    assert len(data["loop"]["rounds"]) == 2


def test_handoff_is_registered_and_old_tone_verbs_are_removed():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "handoff" in result.output
    assert "ship" not in result.output
    assert "boss" not in result.output

    handoff_help = runner.invoke(app, ["handoff", "--help"])
    assert handoff_help.exit_code == 0
    assert "follow-up" in handoff_help.output
    assert "loop" in handoff_help.output

    old_command = runner.invoke(app, ["ship", "--help"])
    assert old_command.exit_code != 0
    assert "No such command" in old_command.output
