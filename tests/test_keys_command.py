from typer.testing import CliRunner

from ax_cli.commands import keys


runner = CliRunner()


class DummyClient:
    def __init__(self):
        self.calls = []

    def create_key(self, name, *, allowed_agent_ids=None, agent_scope=None, agent_id=None):
        self.calls.append(
            {
                "name": name,
                "allowed_agent_ids": allowed_agent_ids,
                "agent_scope": agent_scope,
                "agent_id": agent_id,
            }
        )
        return {"credential_id": "cred-1", "token": "axp_u_test"}

    def list_agents(self):
        return {"agents": []}


def test_create_single_agent_key_sets_bound_agent(monkeypatch):
    client = DummyClient()
    monkeypatch.setattr(keys, "get_client", lambda *, as_user=False: client)

    result = runner.invoke(keys.app, ["create", "--name", "bot", "--agent-id", "agent-123"])

    assert result.exit_code == 0
    assert client.calls == [
        {
            "name": "bot",
            "allowed_agent_ids": ["agent-123"],
            "agent_scope": "agents",
            "agent_id": "agent-123",
        }
    ]
    assert "Bound agent: agent-123" in result.output


def test_create_multi_agent_key_keeps_scope_only(monkeypatch):
    client = DummyClient()
    monkeypatch.setattr(keys, "get_client", lambda *, as_user=False: client)

    result = runner.invoke(
        keys.app,
        ["create", "--name", "multi", "--agent-id", "agent-1", "--agent-id", "agent-2"],
    )

    assert result.exit_code == 0
    assert client.calls == [
        {
            "name": "multi",
            "allowed_agent_ids": ["agent-1", "agent-2"],
            "agent_scope": "agents",
            "agent_id": None,
        }
    ]
    assert "Allowed agents: agent-1, agent-2" in result.output


def test_create_key_supports_as_user_flag(monkeypatch):
    client = DummyClient()
    seen = []

    def fake_get_client(*, as_user=False):
        seen.append(as_user)
        return client

    monkeypatch.setattr(keys, "get_client", fake_get_client)

    result = runner.invoke(keys.app, ["create", "--name", "bot", "--as-user"])

    assert result.exit_code == 0
    assert seen == [True]
