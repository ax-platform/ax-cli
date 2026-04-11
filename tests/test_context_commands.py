from typer.testing import CliRunner

from ax_cli.commands import context
from ax_cli.main import app

runner = CliRunner()


def test_context_download_uses_base_url_and_auth_headers(monkeypatch, tmp_path):
    calls = {}

    class FakeClient:
        base_url = "https://next.paxai.app"

        def get_context(self, key):
            assert key == "image.png"
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

