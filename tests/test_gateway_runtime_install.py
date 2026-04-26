"""Tests for GATEWAY-RUNTIME-AUTOSETUP-001 runtime install endpoint + CLI.

Verifies:
- Allowlist enforcement (only `hermes` today, fail-fast on others)
- Operator-session-required guard
- Home-tree resolution with realpath (symlink trap closed)
- Cleanup on failure (no half-extracted directories left behind)
- CLI ``ax gateway runtime install`` mirrors the endpoint
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ax_cli.commands.gateway import (
    _RUNTIME_INSTALL_RECIPES,
    _install_runtime_payload,
    _resolve_install_target,
)
from ax_cli.main import app

runner = CliRunner()


def test_allowlist_only_hermes_today():
    """Installing anything other than 'hermes' must fail before any subprocess runs."""
    with pytest.raises(ValueError, match="unknown runtime template"):
        _install_runtime_payload("evil", operator_session={"user": "test"})

    with pytest.raises(ValueError, match="unknown runtime template"):
        _install_runtime_payload("ollama", operator_session={"user": "test"})

    # Confirm allowlist is exactly {hermes} so a future addition is a code review
    assert set(_RUNTIME_INSTALL_RECIPES.keys()) == {"hermes"}


def test_operator_session_required():
    """No session → PermissionError before any clone/install runs."""
    with pytest.raises(PermissionError, match="operator session"):
        _install_runtime_payload("hermes", operator_session=None)

    with pytest.raises(PermissionError, match="operator session"):
        _install_runtime_payload("hermes", operator_session={})


def test_target_must_resolve_under_home_tree():
    """Symlink trap: a target that resolves OUTSIDE Path.home() is rejected."""
    # /etc is not under home — direct rejection
    with pytest.raises(ValueError, match="outside home tree"):
        _resolve_install_target("hermes", override="/etc/evil-install")


def test_target_default_is_under_home():
    """Default target ~/hermes-agent resolves cleanly under home tree."""
    target = _resolve_install_target("hermes")
    assert str(target).startswith(str(Path.home().resolve()))
    assert target.name == "hermes-agent"


def test_target_with_user_override_under_home(tmp_path, monkeypatch):
    """An explicit override that's a subdir of home is accepted."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    nested = tmp_path / "subdir" / "custom-hermes"
    target = _resolve_install_target("hermes", override=str(nested))
    assert target == nested.resolve()


def test_install_clone_failure_triggers_cleanup(tmp_path, monkeypatch):
    """If git clone fails, the partial directory we created must be cleaned up."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "hermes-agent"

    # Mock subprocess.run to fail the clone
    def _fake_run(args, **_kw):
        # Simulate clone-time creation then failure
        if args[0] == "git" and args[1] == "clone":
            target.mkdir()
            raise subprocess.CalledProcessError(1, args, stderr="fatal: simulated network error")
        raise AssertionError(f"unexpected subprocess: {args}")

    with patch("ax_cli.commands.gateway.subprocess.run", side_effect=_fake_run):
        result = _install_runtime_payload("hermes", operator_session={"user": "test"})

    assert result["ready"] is False
    assert "clone failed" in result["summary"]
    # Cleanup ran — directory removed
    assert not target.exists()
    # Steps recorded both the failure and the cleanup
    step_names = [s["step"] for s in result["steps"]]
    assert "clone" in step_names
    assert "cleanup" in step_names


def test_install_clone_skipped_when_target_exists(tmp_path, monkeypatch):
    """If target already exists, clone is skipped (idempotent), not failed."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "hermes-agent").mkdir()

    # Subprocess should NOT be called for clone (target exists), and venv/pip
    # may still be invoked. Mock all calls to noop.
    def _fake_run(args, **_kw):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    # Mock hermes_setup_status to return ready
    with patch("ax_cli.commands.gateway.subprocess.run", side_effect=_fake_run), \
         patch("ax_cli.gateway.hermes_setup_status", return_value={"ready": True, "summary": "found"}):
        result = _install_runtime_payload("hermes", operator_session={"user": "test"})

    assert result["ready"] is True
    clone_step = next(s for s in result["steps"] if s["step"] == "clone")
    assert clone_step["status"] == "skipped"


def test_install_full_path_succeeds(tmp_path, monkeypatch):
    """Happy path: clone + venv + pip + verify all succeed → ready=True."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "hermes-agent"

    def _fake_run(args, **_kw):
        # Simulate side effects so subsequent steps can find their inputs
        if args[0] == "git" and args[1] == "clone":
            target.mkdir()
            (target / "pyproject.toml").write_text("[project]\nname='hermes'\n")
        elif args[1:3] == ["-m", "venv"]:
            venv = Path(args[3])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n")
            (venv / "bin" / "pip").chmod(0o755)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("ax_cli.commands.gateway.subprocess.run", side_effect=_fake_run), \
         patch("ax_cli.gateway.hermes_setup_status", return_value={"ready": True, "summary": "ok"}):
        result = _install_runtime_payload("hermes", operator_session={"user": "test"})

    assert result["ready"] is True
    assert "installed at" in result["summary"]
    assert str(target) == result["target"]
    # _log appends; check terminal status per step
    def _terminal(name: str) -> str:
        matches = [s["status"] for s in result["steps"] if s["step"] == name]
        return matches[-1] if matches else ""

    assert _terminal("clone") == "ok"
    assert _terminal("venv") == "ok"
    assert _terminal("pip_install") == "ok"
    assert _terminal("verify") == "ok"


def test_install_pip_failure_is_non_fatal(tmp_path, monkeypatch):
    """pip install -e failure shouldn't tear down the install — clone is still useful."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / "hermes-agent"

    call_count = {"n": 0}

    def _fake_run(args, **_kw):
        call_count["n"] += 1
        if args[0] == "git" and args[1] == "clone":
            target.mkdir()
            (target / "pyproject.toml").write_text("[project]\nname='hermes'\n")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[1:3] == ["-m", "venv"]:
            venv = Path(args[3])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n")
            (venv / "bin" / "pip").chmod(0o755)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        # pip install -e fails
        if "pip" in str(args[0]):
            raise subprocess.CalledProcessError(1, args, stderr="ERROR: simulated pip failure")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("ax_cli.commands.gateway.subprocess.run", side_effect=_fake_run), \
         patch("ax_cli.gateway.hermes_setup_status", return_value={"ready": True, "summary": "found"}):
        result = _install_runtime_payload("hermes", operator_session={"user": "test"})

    # Even though pip failed (warn-level), verify-step succeeded, so ready=True
    assert result["ready"] is True
    # _log appends; pick the terminal status for pip_install
    pip_steps = [s for s in result["steps"] if s["step"] == "pip_install"]
    assert pip_steps[-1]["status"] == "warn"
    assert "non-fatal" in pip_steps[-1]["detail"]
    # Target NOT cleaned up — clone still valuable
    assert target.exists()


def test_cli_install_requires_session(monkeypatch):
    """`ax gateway runtime install` exits 1 with clear error when no session."""
    monkeypatch.setattr("ax_cli.commands.gateway.load_gateway_session", lambda: {})
    result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
    assert result.exit_code != 0
    assert "ax gateway login" in result.output


def test_cli_install_unknown_template(monkeypatch):
    """`ax gateway runtime install evil` exits 1 with allowlist error."""
    monkeypatch.setattr("ax_cli.commands.gateway.load_gateway_session", lambda: {"user": "test"})
    result = runner.invoke(app, ["gateway", "runtime", "install", "evil"])
    assert result.exit_code != 0
    assert "unknown runtime template" in result.output


def test_cli_install_json_output(monkeypatch, tmp_path):
    """`--json` returns the structured install payload."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("ax_cli.commands.gateway.load_gateway_session", lambda: {"user": "test"})

    def _fake_run(args, **_kw):
        if args[0] == "git" and args[1] == "clone":
            target = Path(args[-1])
            target.mkdir()
            (target / "pyproject.toml").write_text("[project]\nname='hermes'\n")
        elif args[1:3] == ["-m", "venv"]:
            venv = Path(args[3])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "pip").write_text("#!/bin/sh\nexit 0\n")
            (venv / "bin" / "pip").chmod(0o755)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with patch("ax_cli.commands.gateway.subprocess.run", side_effect=_fake_run), \
         patch("ax_cli.gateway.hermes_setup_status", return_value={"ready": True, "summary": "ok"}):
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes", "--json"])

    assert result.exit_code == 0, result.output
    import json
    payload = json.loads(result.output)
    assert payload["ready"] is True
    assert "target" in payload
    assert "steps" in payload


def test_cli_status_unknown_template():
    """`ax gateway runtime status` rejects unknown templates."""
    result = runner.invoke(app, ["gateway", "runtime", "status", "evil"])
    assert result.exit_code != 0
    assert "unknown runtime template" in result.output
