"""Tests for the Claude Code channel bridge identity boundary."""

import asyncio

from ax_cli.commands.channel import ChannelBridge


class FakeClient:
    def __init__(self, token: str = "axp_a_AgentKey.Secret", *, agent_id: str = "agent-123"):
        self.token = token
        self.agent_id = agent_id
        self._use_exchange = token.startswith("axp_")
        self.sent = []

    def send_message(self, space_id, content, *, parent_id=None, **kwargs):
        self.sent.append({"space_id": space_id, "content": content, "parent_id": parent_id, **kwargs})
        return {"message": {"id": "msg-123"}}


class CaptureBridge(ChannelBridge):
    def __init__(self, client, *, agent_id="agent-123"):
        super().__init__(
            client=client,
            agent_name="anvil",
            agent_id=agent_id,
            space_id="space-123",
            queue_size=10,
            debug=False,
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
    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "sent reply to incoming-123 (msg-123)"
