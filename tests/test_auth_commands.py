from typer.testing import CliRunner

from ax_cli.commands import auth
from ax_cli.main import app

runner = CliRunner()


def test_login_alias_calls_auth_init(monkeypatch):
    """`ax login` is a top-level alias that forwards to `ax auth init` with identical kwargs."""
    called = {}

    def fake_init(token, base_url, agent, space_id):
        called.update({
            "token": token,
            "base_url": base_url,
            "agent": agent,
            "space_id": space_id,
        })

    monkeypatch.setattr(auth, "init", fake_init)

    result = runner.invoke(
        app,
        [
            "login",
            "--token",
            "axp_u_test.token",
            "--url",
            "https://next.paxai.app",
            "--agent",
            "anvil",
            "--space-id",
            "space-123",
        ],
    )

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://next.paxai.app",
        "agent": "anvil",
        "space_id": "space-123",
    }
