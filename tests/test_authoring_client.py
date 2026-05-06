"""Regression: ``get_authoring_client()`` is the single entry point for a
credential-bearing AxClient and resolves correctly across the two
credential paths.

Per issue #142 (Phase 3 of the Gateway-as-MCP plan), every command that
talks to aX or the local Gateway should obtain its client through one
factory. This module locks the resolver behavior:

  - In a Gateway-brokered workspace (``[gateway]`` + ``[agent]`` blocks in
    ``.ax/config.toml`` and a registry entry for the agent), the factory
    returns a managed-agent AxClient. This is what unblocks
    ``ax tasks create`` from a Gateway-managed shell with no local PAT.
  - In a non-brokered workspace with a local PAT, the factory returns the
    same client ``get_client()`` would have returned. Behavior preserved.
  - With neither a brokered registry entry nor a local credential, the
    factory exits with ``get_client()``'s actionable error rather than
    silently falling back.

Phase 4 will swap the implementation behind this entry point. These
tests pin the contract Phase 4 must preserve.
"""

from __future__ import annotations

import pytest
import typer

from ax_cli import config as ax_config
from ax_cli import gateway as gateway_core
from ax_cli.client import AxClient

# ---- Fixture helpers (shape mirrors tests/test_agents_test_invoking_principal.py) ----


def _make_registry_agent(*, name, agent_id, token_file, space_id="space-1"):
    return {
        "name": name,
        "agent_id": agent_id,
        "space_id": space_id,
        "active_space_id": space_id,
        "default_space_id": space_id,
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "template_id": "echo_test",
        "desired_state": "running",
        "effective_state": "running",
        "transport": "gateway",
        "credential_source": "gateway",
        "allowed_spaces": [{"space_id": space_id, "name": "Test Space", "is_default": True}],
        "token_file": str(token_file),
    }


def _seed_session_and_registry(tmp_path, monkeypatch, *, agents):
    config_dir = tmp_path / "_gw_config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "operator",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = list(agents)
    for entry in registry["agents"]:
        gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)


def _write_workspace_gateway_config(tmp_path, *, agent_name):
    """Write a workspace-local ``.ax/config.toml`` so ``resolve_gateway_config()``
    sees the workspace as Gateway-brokered.

    Uses TOML *literal* strings (single quotes) for paths so Windows
    backslashes (``C:\\Users\\...``) aren't parsed as escape sequences.
    """
    local_ax = tmp_path / ".ax"
    local_ax.mkdir(exist_ok=True)
    (local_ax / "config.toml").write_text(
        "[gateway]\n"
        'mode = "local"\n'
        'url = "http://127.0.0.1:8765"\n'
        "\n"
        "[agent]\n"
        f"agent_name = '{agent_name}'\n"
        f"workdir = '{tmp_path}'\n"
    )


def _clear_token_env(monkeypatch):
    """Strip every env var the legacy resolver consults so a 'no PAT' shell
    really means no PAT. Order matters here — ``get_client()`` checks several."""
    for var in (
        "AX_TOKEN",
        "AX_API_TOKEN",
        "AX_USER_TOKEN",
        "AX_AGENT_TOKEN",
        "AX_PAT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---- Test 1: Gateway-brokered workspace returns a managed-agent client ----


def test_brokered_workspace_returns_managed_agent_client(tmp_path, monkeypatch):
    """A workspace with ``[gateway]`` + ``[agent]`` blocks and a matching
    registry entry must yield the managed-agent AxClient — no local PAT
    required. This is the core unblock for ``ax tasks create`` from a
    Gateway-managed shell."""
    agent_token = tmp_path / "cli_god.token"
    agent_token.write_text("axp_a_managed.secret")
    _seed_session_and_registry(
        tmp_path,
        monkeypatch,
        agents=[
            _make_registry_agent(name="cli_god", agent_id="agent-cli-god", token_file=agent_token),
        ],
    )
    _write_workspace_gateway_config(tmp_path, agent_name="cli_god")
    monkeypatch.chdir(tmp_path)
    _clear_token_env(monkeypatch)

    client = ax_config.get_authoring_client()

    assert isinstance(client, AxClient)
    # The managed-agent client carries the agent's identity, not a user PAT
    assert client.agent_name == "cli_god"
    assert client.agent_id == "agent-cli-god"
    # Token came from the registry entry's on-disk token file
    assert client.token == "axp_a_managed.secret"


# ---- Test 2: Non-brokered workspace returns the legacy client unchanged ----


def test_non_brokered_workspace_returns_legacy_client(tmp_path, monkeypatch):
    """Without a ``[gateway]`` block, the factory must defer to ``get_client()``
    and return whatever it returns. Behavior preserved for every shell that
    isn't Gateway-managed."""
    monkeypatch.chdir(tmp_path)
    _clear_token_env(monkeypatch)
    monkeypatch.setenv("AX_TOKEN", "axp_a_legacy.secret")
    monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")

    client = ax_config.get_authoring_client()

    assert isinstance(client, AxClient)
    assert client.token == "axp_a_legacy.secret"
    assert client.base_url == "https://paxai.app"


# ---- Test 3: Neither path resolves -> clear, actionable error (no silent fallback) ----


def test_no_brokered_entry_and_no_token_raises_clear_error(tmp_path, monkeypatch, capsys):
    """Workspace declares Gateway-brokered, but no registry entry and no
    local PAT. The factory must propagate ``get_client()``'s actionable
    error rather than silently returning anything."""
    _seed_session_and_registry(tmp_path, monkeypatch, agents=[])  # empty registry
    _write_workspace_gateway_config(tmp_path, agent_name="missing_agent")
    monkeypatch.chdir(tmp_path)
    _clear_token_env(monkeypatch)

    with pytest.raises(typer.Exit) as excinfo:
        ax_config.get_authoring_client()

    # ``get_client()`` exits with code 1 and an operator-actionable message
    assert excinfo.value.exit_code == 1
    err_text = capsys.readouterr().err.lower()
    assert "no api credential" in err_text or "no credential" in err_text
    # The error should point the operator at the Gateway path explicitly
    assert "gateway" in err_text


# ---- Bonus: backwards-compat sanity — get_client still works as before ----


def test_get_client_preserved_for_backwards_compat(tmp_path, monkeypatch):
    """Issue #142 explicitly requires ``get_client`` not be renamed or
    removed. This pins that any code still importing ``get_client`` keeps
    working."""
    monkeypatch.chdir(tmp_path)
    _clear_token_env(monkeypatch)
    monkeypatch.setenv("AX_TOKEN", "axp_a_legacy.secret")
    monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")

    legacy = ax_config.get_client()

    assert isinstance(legacy, AxClient)
    assert legacy.token == "axp_a_legacy.secret"
