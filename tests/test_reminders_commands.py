"""Tests for the local reminder policy runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(
        self,
        space_id: str,
        content: str,
        *,
        channel: str = "main",
        metadata: dict | None = None,
        message_type: str = "text",
        **_kwargs: Any,
    ) -> dict:
        message_id = f"msg-{len(self.sent) + 1}"
        self.sent.append(
            {
                "id": message_id,
                "space_id": space_id,
                "content": content,
                "channel": channel,
                "metadata": metadata,
                "message_type": message_type,
            }
        )
        return {"id": message_id}


def _install_fake_runtime(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.reminders.get_client", lambda: client)
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_space_id",
        lambda _client, *, explicit=None: explicit or "space-abc",
    )
    monkeypatch.setattr(
        "ax_cli.commands.reminders.resolve_agent_name",
        lambda client=None: "chatgpt",
    )


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_add_creates_local_policy_file(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"

    result = runner.invoke(
        app,
        [
            "reminders",
            "add",
            "task-1",
            "--reason",
            "check this task",
            "--target",
            "orion",
            "--first-in-minutes",
            "0",
            "--max-fires",
            "2",
            "--file",
            str(policy_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    store = _load(policy_file)
    assert store["version"] == 1
    assert len(store["policies"]) == 1
    policy = store["policies"][0]
    assert policy["source_task_id"] == "task-1"
    assert policy["reason"] == "check this task"
    assert policy["target"] == "orion"
    assert policy["max_fires"] == 2
    assert policy["enabled"] is True


def test_run_once_fires_due_policy_and_disables_at_max(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-test",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "review task state",
                        "target": "orion",
                        "severity": "info",
                        "cadence_seconds": 300,
                        "next_fire_at": "2026-04-16T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert sent["message_type"] == "reminder"
    assert sent["content"].startswith("@orion Reminder:")
    metadata = sent["metadata"]
    assert metadata["alert"]["kind"] == "task_reminder"
    assert metadata["alert"]["source_task_id"] == "task-1"
    assert metadata["alert"]["target_agent"] == "orion"
    assert metadata["alert"]["response_required"] is True
    assert metadata["reminder_policy"]["policy_id"] == "rem-test"

    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is False
    assert stored["disabled_reason"] == "max_fires reached"
    assert stored["fired_count"] == 1
    assert stored["last_message_id"] == "msg-1"


def test_run_once_skips_future_policy(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_fake_runtime(monkeypatch, fake)
    policy_file = tmp_path / "reminders.json"
    policy_file.write_text(
        json.dumps(
            {
                "version": 1,
                "policies": [
                    {
                        "id": "rem-future",
                        "enabled": True,
                        "space_id": "space-abc",
                        "source_task_id": "task-1",
                        "reason": "not yet",
                        "target": "orion",
                        "cadence_seconds": 300,
                        "next_fire_at": "2999-01-01T00:00:00Z",
                        "max_fires": 1,
                        "fired_count": 0,
                        "fired_keys": [],
                    }
                ],
            }
        )
    )

    result = runner.invoke(app, ["reminders", "run", "--once", "--file", str(policy_file), "--json"])

    assert result.exit_code == 0, result.output
    assert fake.sent == []
    stored = _load(policy_file)["policies"][0]
    assert stored["enabled"] is True
    assert stored["fired_count"] == 0
