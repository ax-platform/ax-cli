import pytest
from click.exceptions import Exit

from ax_cli.commands import auth


def test_bind_saves_global_agent_binding(tmp_path, monkeypatch):
    global_dir = tmp_path / "global_ax"
    global_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))

    auth.bind(
        agent="orion",
        agent_id="70c1b445-c733-44d8-8e75-9620452374a8",
        space_id="space-123",
        local=False,
    )

    saved = (global_dir / "config.toml").read_text()
    assert 'agent_name = "orion"' in saved
    assert 'agent_id = "70c1b445-c733-44d8-8e75-9620452374a8"' in saved
    assert 'space_id = "space-123"' in saved


def test_bind_requires_agent_or_agent_id():
    with pytest.raises(Exit):
        auth.bind(agent=None, agent_id=None, space_id=None, local=False)
