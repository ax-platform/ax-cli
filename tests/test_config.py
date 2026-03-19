from pathlib import Path

from ax_cli import config


def test_local_config_dir_prefers_existing_ax_without_git(tmp_path, monkeypatch):
    root = tmp_path / "project"
    nested = root / "services" / "agent"
    (root / ".ax").mkdir(parents=True)
    nested.mkdir(parents=True)

    monkeypatch.chdir(nested)

    assert config._local_config_dir() == root / ".ax"


def test_save_local_config_creates_ax_in_non_git_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config._save_config({"token": "axp_u_test"}, local=True)

    assert (tmp_path / ".ax" / "config.toml").exists()


def test_get_client_prefers_agent_name_over_agent_id_from_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    ax_dir = project / ".ax"
    ax_dir.mkdir(parents=True)
    (ax_dir / "config.toml").write_text(
        'token = "axp_u_test"\n'
        'base_url = "https://dev.paxai.app"\n'
        'agent_name = "orion"\n'
        'agent_id = "70c1b445-c733-44d8-8e75-9620452374a8"\n'
    )
    monkeypatch.chdir(project)

    client = config.get_client()

    assert client._headers["X-Agent-Name"] == "orion"
    assert "X-Agent-Id" not in client._headers


def test_get_client_warns_and_ignores_agent_id_when_name_env_is_set(monkeypatch, capsys):
    monkeypatch.setenv("AX_TOKEN", "axp_u_test")
    monkeypatch.setenv("AX_BASE_URL", "https://dev.paxai.app")
    monkeypatch.setenv("AX_AGENT_NAME", "orion")
    monkeypatch.setenv("AX_AGENT_ID", "70c1b445-c733-44d8-8e75-9620452374a8")

    client = config.get_client()
    captured = capsys.readouterr()

    assert "using AX_AGENT_NAME and ignoring AX_AGENT_ID" in captured.err
    assert client._headers["X-Agent-Name"] == "orion"
    assert "X-Agent-Id" not in client._headers
