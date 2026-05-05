"""Tests for the non-blocking pending-reply warning on send paths."""

import json
import re

from typer.testing import CliRunner

from ax_cli.commands.messages import (
    augment_send_receipt_with_pending,
    check_pending_replies,
    print_pending_reply_warning,
)
from ax_cli.main import app

runner = CliRunner()
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


# ---------- check_pending_replies ----------


class _StubClient:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def list_messages(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_check_pending_replies_empty_when_no_messages():
    client = _StubClient({"messages": []})
    pending = check_pending_replies(client=client, space_id="s1")
    assert pending == {"count": 0, "message_ids": [], "newest_senders": []}
    assert client.calls == [{"limit": 5, "channel": "main", "unread_only": True, "space_id": "s1"}]


def test_check_pending_replies_uses_messages_with_unread_only_filter():
    client = _StubClient(
        {
            "messages": [
                {"id": "m1", "display_name": "alice"},
                {"id": "m2", "display_name": "bob"},
            ],
            "unread_count": 7,
        }
    )
    pending = check_pending_replies(client=client)
    assert pending["count"] == 7
    assert pending["message_ids"] == ["m1", "m2"]
    assert pending["newest_senders"] == ["alice", "bob"]


def test_check_pending_replies_dedupes_senders_preserving_newest_order():
    client = _StubClient(
        {
            "messages": [
                {"id": "m1", "display_name": "alice"},
                {"id": "m2", "display_name": "alice"},
                {"id": "m3", "display_name": "bob"},
                {"id": "m4", "sender_handle": "carol"},
            ],
        }
    )
    pending = check_pending_replies(client=client)
    assert pending["newest_senders"] == ["alice", "bob", "carol"]


def test_check_pending_replies_falls_back_to_message_count_without_unread_count():
    client = _StubClient({"messages": [{"id": "m1"}, {"id": "m2"}]})
    pending = check_pending_replies(client=client)
    assert pending["count"] == 2
    assert pending["message_ids"] == ["m1", "m2"]


def test_check_pending_replies_swallows_exceptions():
    client = _StubClient(RuntimeError("boom"))
    pending = check_pending_replies(client=client)
    assert pending == {"count": 0, "message_ids": [], "newest_senders": []}


def test_check_pending_replies_returns_empty_without_client_or_gateway_cfg():
    pending = check_pending_replies()
    assert pending == {"count": 0, "message_ids": [], "newest_senders": []}


def test_check_pending_replies_uses_gateway_local_call(monkeypatch):
    captured = {}

    def fake_local_call(*, gateway_cfg, method, args, space_id):
        captured["gateway_cfg"] = gateway_cfg
        captured["method"] = method
        captured["args"] = args
        captured["space_id"] = space_id
        return {"messages": [{"id": "g1", "display_name": "gw-sender"}]}

    monkeypatch.setattr(
        "ax_cli.commands.messages._gateway_local_call",
        fake_local_call,
    )
    pending = check_pending_replies(gateway_cfg={"mode": "local"}, space_id="s9")
    assert captured["method"] == "list_messages"
    assert captured["args"] == {"limit": 5, "channel": "main", "unread_only": True, "space_id": "s9"}
    assert captured["space_id"] == "s9"
    assert pending["count"] == 1
    assert pending["message_ids"] == ["g1"]
    assert pending["newest_senders"] == ["gw-sender"]


def test_check_pending_replies_swallows_gateway_local_call_errors(monkeypatch):
    def fake_local_call(**_kwargs):
        raise RuntimeError("gateway dead")

    monkeypatch.setattr("ax_cli.commands.messages._gateway_local_call", fake_local_call)
    pending = check_pending_replies(gateway_cfg={"mode": "local"})
    assert pending == {"count": 0, "message_ids": [], "newest_senders": []}


# ---------- print_pending_reply_warning ----------


def test_print_pending_reply_warning_prints_nothing_when_count_zero(capsys):
    print_pending_reply_warning({"count": 0, "message_ids": [], "newest_senders": []})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_print_pending_reply_warning_singular_with_one_sender(capsys):
    print_pending_reply_warning({"count": 1, "message_ids": ["x"], "newest_senders": ["alice"]})
    out = _strip_ansi(capsys.readouterr().out)
    # Rich may wrap the output; collapse whitespace for assertions.
    flat = " ".join(out.split())
    assert "1 pending reply" in flat
    assert "newest from @alice" in flat
    assert "ax messages list --unread" in flat


def test_print_pending_reply_warning_plural_with_multiple_senders(capsys):
    print_pending_reply_warning(
        {"count": 4, "message_ids": ["x"], "newest_senders": ["alice", "bob", "carol"]}
    )
    flat = " ".join(_strip_ansi(capsys.readouterr().out).split())
    assert "4 pending replies" in flat
    assert "newest from @alice" in flat
    assert "+2 others" in flat


def test_print_pending_reply_warning_handles_non_dict_gracefully(capsys):
    print_pending_reply_warning(None)  # type: ignore[arg-type]
    assert capsys.readouterr().out == ""


# ---------- augment_send_receipt_with_pending ----------


def test_augment_send_receipt_adds_three_fields():
    receipt = {"id": "m1"}
    out = augment_send_receipt_with_pending(
        receipt,
        {"count": 3, "message_ids": ["a", "b"], "newest_senders": ["alice"]},
    )
    assert out["pending_reply_count"] == 3
    assert out["pending_reply_message_ids"] == ["a", "b"]
    assert out["pending_reply_newest_senders"] == ["alice"]
    # mutates in place
    assert receipt is out


def test_augment_send_receipt_safe_with_non_dict_inputs():
    assert augment_send_receipt_with_pending("not a dict", {}) == "not a dict"  # type: ignore[arg-type]
    receipt = {"id": "m1"}
    assert augment_send_receipt_with_pending(receipt, None) is receipt  # type: ignore[arg-type]
    assert "pending_reply_count" not in receipt


# ---------- send-path integration: ax messages send / ax send ----------


class _FakeSendClient:
    """Captures send + list_messages calls for the direct (non-gateway) send path."""

    def __init__(self, *, unread_messages=None, send_response=None):
        self._base_headers = {}
        self._unread = unread_messages or []
        self._send_response = send_response or {"id": "sent-1", "message": {"id": "sent-1"}}
        self.list_messages_calls = 0
        self.send_calls = 0

    def whoami(self):
        return {"agent_name": "cli_god", "credential_scope": {"agent_scope": "all"}}

    def list_spaces(self):
        return [{"id": "space-1", "name": "test space"}]

    def list_messages(self, **kwargs):
        self.list_messages_calls += 1
        if not kwargs.get("unread_only"):
            return {"messages": []}
        return {"messages": self._unread, "unread_count": len(self._unread)}

    def send_message(self, space_id, content, **kwargs):
        self.send_calls += 1
        return self._send_response


def _patch_send_client(monkeypatch, fake_client):
    monkeypatch.setattr("ax_cli.commands.messages.get_client", lambda: fake_client)
    monkeypatch.setattr("ax_cli.commands.messages.resolve_gateway_config", lambda: None)
    monkeypatch.setattr("ax_cli.commands.messages.resolve_space_id", lambda *a, **kw: "space-1")
    monkeypatch.setattr("ax_cli.commands.messages.resolve_agent_name", lambda **kw: "cli_god")


def test_ax_send_warns_about_pending_replies_in_text_output(monkeypatch):
    fake = _FakeSendClient(
        unread_messages=[{"id": "u1", "display_name": "boss"}],
    )
    _patch_send_client(monkeypatch, fake)

    result = runner.invoke(app, ["send", "hello", "--no-wait"])
    assert result.exit_code == 0, result.output
    flat = " ".join(_strip_ansi(result.output).split())
    assert "Sent." in flat
    assert "1 pending reply" in flat
    assert "newest from @boss" in flat
    assert fake.send_calls == 1


def test_ax_send_no_warning_when_no_pending_replies(monkeypatch):
    fake = _FakeSendClient(unread_messages=[])
    _patch_send_client(monkeypatch, fake)

    result = runner.invoke(app, ["send", "hello", "--no-wait"])
    assert result.exit_code == 0, result.output
    flat = " ".join(_strip_ansi(result.output).split())
    assert "Sent." in flat
    assert "pending repl" not in flat


def test_ax_send_json_includes_pending_reply_fields(monkeypatch):
    fake = _FakeSendClient(
        unread_messages=[
            {"id": "u1", "display_name": "boss"},
            {"id": "u2", "display_name": "ops"},
        ],
    )
    _patch_send_client(monkeypatch, fake)

    result = runner.invoke(app, ["send", "hello", "--no-wait", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pending_reply_count"] == 2
    assert payload["pending_reply_message_ids"] == ["u1", "u2"]
    assert payload["pending_reply_newest_senders"] == ["boss", "ops"]


def test_ax_send_json_zero_pending_when_clean(monkeypatch):
    fake = _FakeSendClient(unread_messages=[])
    _patch_send_client(monkeypatch, fake)

    result = runner.invoke(app, ["send", "hello", "--no-wait", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pending_reply_count"] == 0
    assert payload["pending_reply_message_ids"] == []
    assert payload["pending_reply_newest_senders"] == []


def test_ax_send_does_not_block_when_pending_reply_check_errors(monkeypatch):
    """Pending-reply check errors must never block a send."""

    class _BoomClient(_FakeSendClient):
        def list_messages(self, **kwargs):
            if kwargs.get("unread_only"):
                raise RuntimeError("network unavailable")
            return {"messages": []}

    fake = _BoomClient()
    _patch_send_client(monkeypatch, fake)

    result = runner.invoke(app, ["send", "hello", "--no-wait"])
    assert result.exit_code == 0, result.output
    flat = " ".join(_strip_ansi(result.output).split())
    assert "Sent." in flat
    # Warning suppressed, send still delivered.
    assert "pending repl" not in flat
    assert fake.send_calls == 1
