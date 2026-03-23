import httpx
import pytest

from ax_cli.client import AxClient
from ax_cli.commands import messages
from ax_cli.commands.messages import (
    _configure_send_identity,
    _format_received_at,
    _mentioned_agents,
    _message_list_row,
    _sender_label,
    _summary_text,
)


def test_configure_send_identity_keeps_default_when_no_flags():
    """No explicit flags → preserve whatever get_client() resolved.

    Agent-scoped PATs need their identity header to avoid 403.
    """
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    _configure_send_identity(client, agent_name=None, agent_id=None)

    # Default identity is preserved, not stripped
    assert client._headers["X-Agent-Id"] == "70c1b445-c733-44d8-8e75-9620452374a8"


def test_configure_send_identity_keeps_default_name_when_no_flags():
    """Bootstrap client with only name — preserved when no flags."""
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    _configure_send_identity(client, agent_name=None, agent_id=None)

    assert client._headers["X-Agent-Name"] == "orion"


def test_configure_send_identity_overrides_with_explicit_agent_name():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
    )

    _configure_send_identity(client, agent_name="canvas", agent_id=None)

    assert client._headers["X-Agent-Name"] == "canvas"
    assert "X-Agent-Id" not in client._headers


def test_configure_send_identity_overrides_with_explicit_agent_id():
    client = AxClient(
        "https://dev.paxai.app",
        "axp_u_test",
        agent_name="orion",
    )

    _configure_send_identity(client, agent_name=None, agent_id="new-id")

    assert client._headers["X-Agent-Id"] == "new-id"
    assert "X-Agent-Name" not in client._headers


def test_send_surfaces_stale_binding_without_retry(monkeypatch):
    calls = []

    def fake_get_client(*, as_user=False):
        return {"as_user": as_user}

    def fake_send_once(**kwargs):
        calls.append(kwargs["client"]["as_user"])
        if not kwargs["client"]["as_user"]:
            request = httpx.Request("POST", "https://dev.paxai.app/api/v1/messages")
            response = httpx.Response(
                403,
                request=request,
                json={"detail": "Agent not permitted by this credential's allowed_agent_ids"},
            )
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)
        return {"id": "msg-1"}

    handled = []

    def fake_handle_error(exc):
        handled.append(str(exc.response.json()["detail"]))
        raise SystemExit(1)

    monkeypatch.setattr(messages, "get_client", fake_get_client)
    monkeypatch.setattr(messages, "_send_once", fake_send_once)
    monkeypatch.setattr(messages, "handle_error", fake_handle_error)

    with pytest.raises(SystemExit):
        messages.send(
            content="hello",
            wait=False,
            timeout=1,
            agent_id=None,
            agent_name=None,
            as_user=False,
            channel="main",
            parent=None,
            space_id="space-123",
            as_json=False,
        )

    assert calls == [False]
    assert handled == ["Agent not permitted by this credential's allowed_agent_ids"]


def test_sender_label_prefers_display_name():
    message = {
        "display_name": "backend_sentinel",
        "sender_handle": "@backend_sentinel",
        "sender_type": "agent",
    }

    assert _sender_label(message) == "backend_sentinel"


def test_mentions_summary_prefers_original_mentions():
    message = {
        "metadata": {
            "original_mentions": ["aX", "claude-code"],
            "mentions": [{"agent_name": "ignored"}],
        }
    }

    assert _mentioned_agents(message) == "@aX, @claude-code"


def test_summary_text_prefers_ai_summary():
    message = {
        "ai_summary": "This is the model summary.",
        "content": "Raw message content that should not win.",
    }

    assert _summary_text(message) == "This is the model summary."


def test_format_received_at_uses_utc_label():
    message = {"created_at": "2026-03-22T06:22:24.608914+00:00"}

    assert _format_received_at(message) == "2026-03-22 06:22 UTC"


def test_message_list_row_builds_summary_view():
    message = {
        "id": "msg-123",
        "display_name": "claude-code",
        "metadata": {"original_mentions": ["aX"]},
        "ai_summary": "Deploy triggered after migration fix merged.",
        "created_at": "2026-03-22T06:13:43.543397+00:00",
    }

    assert _message_list_row(message) == {
        "id": "msg-123",
        "from": "claude-code",
        "mentions": "@aX",
        "summary": "Deploy triggered after migration fix merged.",
        "received": "2026-03-22 06:13 UTC",
    }
