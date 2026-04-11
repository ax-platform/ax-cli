from typer.testing import CliRunner

from ax_cli.context_keys import build_upload_context_key
from ax_cli.commands import context
from ax_cli.main import app

runner = CliRunner()


def test_context_download_uses_base_url_and_auth_headers(monkeypatch, tmp_path):
    calls = {}

    class FakeClient:
        base_url = "https://next.paxai.app"

        def get_context(self, key, *, space_id=None):
            assert key == "image.png"
            assert space_id == "space-1"
            return {
                "value": {
                    "type": "file_upload",
                    "filename": "image.png",
                    "url": "/api/v1/uploads/files/image.png",
                }
            }

        def _auth_headers(self):
            return {
                "Authorization": "Bearer exchanged.jwt",
                "Content-Type": "application/json",
                "X-AX-FP": "fp",
            }

    class FakeResponse:
        content = b"png-bytes"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self, *, headers, timeout, follow_redirects):
            calls["headers"] = headers
            calls["timeout"] = timeout
            calls["follow_redirects"] = follow_redirects

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def get(self, url):
            calls["url"] = url
            return FakeResponse()

    monkeypatch.setattr(context, "get_client", lambda: FakeClient())
    monkeypatch.setattr(context, "resolve_space_id", lambda client, explicit=None: "space-1")
    monkeypatch.setattr(context.httpx, "Client", FakeHttpClient)

    output = tmp_path / "downloaded.png"
    result = runner.invoke(app, ["context", "download", "image.png", "--output", str(output)])

    assert result.exit_code == 0
    assert output.read_bytes() == b"png-bytes"
    assert calls["url"] == "https://next.paxai.app/api/v1/uploads/files/image.png"
    assert calls["headers"] == {
        "Authorization": "Bearer exchanged.jwt",
        "X-AX-FP": "fp",
    }
    assert calls["follow_redirects"] is True


def test_default_upload_context_key_is_unique(monkeypatch):
    monkeypatch.setattr("ax_cli.context_keys.time.time", lambda: 1775880839.429)

    first = build_upload_context_key("image.png", "df9b1d15-e9c5-4e60-851e-53ea35b4f5e7")
    second = build_upload_context_key("image.png", "774758d4-8451-4570-bca4-e4c4d34706ac")

    assert first == "upload:1775880839429:image.png:df9b1d15-e9c5-4e60-851e-53ea35b4f5e7"
    assert second == "upload:1775880839429:image.png:774758d4-8451-4570-bca4-e4c4d34706ac"
    assert first != second
