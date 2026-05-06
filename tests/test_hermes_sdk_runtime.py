"""Tests for the Hermes SDK runtime adapter.

Currently focused on Windows-vs-POSIX permission warning parity — the
adapter's `_read_token_file` used to log a "loose permissions" warning on
every call against Windows because NTFS reports POSIX mode bits as
0o666/0o644 regardless of the file's actual ACLs. This is the same class
of bug fixed for `ax_cli/token_cache.py` and `ax_cli/config.py` upstream;
the Hermes adapter was a deferred sibling.
"""

from __future__ import annotations

import logging
import sys

import pytest

from ax_cli.runtimes.hermes.runtimes import hermes_sdk


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_read_token_file_warns_on_loose_permissions_posix(tmp_path, caplog):
    token = tmp_path / "codex-token"
    token.write_text("axt_loose")
    token.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="runtime.hermes_sdk"):
        result = hermes_sdk._read_token_file(token)

    assert result == "axt_loose"
    assert any("loose permissions" in record.message for record in caplog.records)


def test_read_token_file_skips_permission_warning_on_windows(tmp_path, caplog, monkeypatch):
    """On Windows the mode check would warn on every read — the guard must suppress it."""
    monkeypatch.setattr(hermes_sdk.sys, "platform", "win32")
    token = tmp_path / "codex-token"
    token.write_text("axt_winsafe")
    if sys.platform != "win32":
        token.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="runtime.hermes_sdk"):
        result = hermes_sdk._read_token_file(token)

    assert result == "axt_winsafe"
    assert not any("loose permissions" in record.message for record in caplog.records), [
        record.message for record in caplog.records
    ]


def test_read_token_file_returns_empty_on_missing_path(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert hermes_sdk._read_token_file(missing) == ""
