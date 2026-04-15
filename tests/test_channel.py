"""Tests for the Claude Code channel bridge identity boundary."""

import asyncio
import os

from ax_cli.commands.channel import ChannelBridge, _load_channel_env
from ax_cli.commands.listen import _is_self_authored, _remember_reply_anchor, _should_respond


class FakeClient:
    def __init__(self, token: str = "axp_a_AgentKey.Secret", *, agent_id: str = "agent-123"):
        self.token = token
        self.agent_id = agent_id
        self._use_exchange = token.startswith("axp_")
        self.sent = []
        self.processing_statuses = []

    def send_message(self, space_id, content, *, parent_id=None, **kwargs):
        self.sent.append({"space_id": space_id, "content": content, "parent_id": parent_id, **kwargs})
        return {"message": {"id": "msg-123"}}

    def set_agent_processing_status(self, message_id, status, *, agent_name=None, space_id=None):
        self.processing_statuses.append(
            {
                "message_id": message_id,
                "status": status,
                "agent_name": agent_name,
                "space_id": space_id,
            }
        )
        return {"ok": True, "status": status}


class CaptureBridge(ChannelBridge):
    def __init__(self, client, *, agent_id="agent-123", processing_status=True):
        super().__init__(
            client=client,
            agent_name="anvil",
            agent_id=agent_id,
            space_id="space-123",
            queue_size=10,
            debug=False,
            processing_status=processing_status,
        )
        self.writes = []

    async def write_message(self, payload):
        self.writes.append(payload)


def test_channel_rejects_user_pat_for_agent_reply():
    client = FakeClient("axp_u_UserKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello"}},
        )
    )

    assert client.sent == []
    result = bridge.writes[0]["result"]
    assert result["isError"] is True
    assert "agent-bound PAT" in result["content"][0]["text"]


def test_channel_sends_with_agent_bound_pat():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello"}},
        )
    )

    assert client.sent == [{"space_id": "space-123", "content": "hello", "parent_id": "incoming-123"}]
    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "completed",
            "agent_name": "anvil",
            "space_id": "space-123",
        }
    ]
    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "sent reply to incoming-123 (msg-123)"
    assert "msg-123" in bridge._reply_anchor_ids


def test_channel_can_publish_working_status_on_delivery():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "working",
            "agent_name": "anvil",
            "space_id": "space-123",
        }
    ]


def test_channel_processing_status_can_be_disabled():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client, processing_status=False)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == []


def test_channel_returns_empty_optional_mcp_lists():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_request({"id": 1, "method": "resources/list"}))
    asyncio.run(bridge.handle_request({"id": 2, "method": "resources/templates/list"}))
    asyncio.run(bridge.handle_request({"id": 3, "method": "prompts/list"}))

    assert bridge.writes == [
        {"jsonrpc": "2.0", "id": 1, "result": {"resources": []}},
        {"jsonrpc": "2.0", "id": 2, "result": {"resourceTemplates": []}},
        {"jsonrpc": "2.0", "id": 3, "result": {"prompts": []}},
    ]


def test_channel_env_file_sets_missing_runtime_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AX_CONFIG_FILE=/tmp/agent/.ax/config.toml\n"
        "AX_SPACE_ID=space-123\n"
        "AX_AGENT_NAME=ignored-agent\n"
    )
    monkeypatch.setenv("AX_AGENT_NAME", "existing-agent")

    _load_channel_env(env_file)

    assert os.environ["AX_CONFIG_FILE"] == "/tmp/agent/.ax/config.toml"
    assert os.environ["AX_SPACE_ID"] == "space-123"
    assert os.environ["AX_AGENT_NAME"] == "existing-agent"


def test_listener_treats_parent_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "parent_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "orion", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "anvil", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_treats_conversation_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "conversation_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "orion", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "anvil", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_tracks_self_authored_messages_without_responding():
    anchors: set[str] = set()
    data = {
        "id": "agent-message-1",
        "content": "@orion please check this",
        "author": {"id": "agent-123", "name": "anvil", "type": "agent"},
        "mentions": ["orion"],
    }

    assert _is_self_authored(data, "anvil", "agent-123") is True
    _remember_reply_anchor(anchors, data["id"])
    assert _should_respond(data, "anvil", "agent-123", reply_anchor_ids=anchors) is False
    assert anchors == {"agent-message-1"}
