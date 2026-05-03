"""Tests for ax_cli.output.handle_error operator hints."""

import httpx
import pytest
import typer

from ax_cli.output import handle_error


def _http_error(
    status_code: int, url: str, *, detail: str = "invalid_credential"
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status_code, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def test_handle_error_401_at_auth_exchange_hints_doctor(capsys):
    err = _http_error(401, "https://paxai.app/auth/exchange")
    with pytest.raises(typer.Exit):
        handle_error(err)
    captured = capsys.readouterr()
    assert "ax auth doctor" in captured.err


def test_handle_error_429_at_auth_exchange_hints_doctor(capsys):
    err = _http_error(429, "https://paxai.app/auth/exchange", detail="rate_limited")
    with pytest.raises(typer.Exit):
        handle_error(err)
    captured = capsys.readouterr()
    assert "ax auth doctor" in captured.err


def test_handle_error_401_on_business_endpoint_does_not_hint_doctor(capsys):
    err = _http_error(401, "https://paxai.app/api/v1/messages")
    with pytest.raises(typer.Exit):
        handle_error(err)
    captured = capsys.readouterr()
    assert "ax auth doctor" not in captured.err


def test_handle_error_404_at_auth_exchange_does_not_hint_doctor(capsys):
    err = _http_error(404, "https://paxai.app/auth/exchange", detail="not_found")
    with pytest.raises(typer.Exit):
        handle_error(err)
    captured = capsys.readouterr()
    assert "ax auth doctor" not in captured.err
