from ax_cli.client import AxClient


def test_client_uses_only_name_when_both_are_provided():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    assert client._headers["X-Agent-Name"] == "orion"
    assert "X-Agent-Id" not in client._headers


def test_explicit_agent_id_override_replaces_default_name_header():
    client = AxClient("https://dev.paxai.app", "axp_u_test", agent_name="orion")

    headers = client._with_agent("82d4765a-b2fc-4959-9765-d04d0b654fd0")

    assert headers["X-Agent-Id"] == "82d4765a-b2fc-4959-9765-d04d0b654fd0"
    assert "X-Agent-Name" not in headers
