from ax_cli.client import AxClient
from ax_cli.commands.messages import _configure_send_identity


def test_configure_send_identity_clears_default_agent_for_user_sends():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    _configure_send_identity(client, agent_name=None, agent_id=None)

    assert "X-Agent-Name" not in client._headers
    assert "X-Agent-Id" not in client._headers


def test_configure_send_identity_preserves_explicit_agent_name():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    _configure_send_identity(client, agent_name="canvas", agent_id=None)

    assert client._headers["X-Agent-Name"] == "canvas"
    assert "X-Agent-Id" not in client._headers
