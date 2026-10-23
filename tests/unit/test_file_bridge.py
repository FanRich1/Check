import hashlib
import time

import httpx
import pytest

from shipping_billing.ocr.file_bridge import TemporaryFileServer


def test_exact_file_only_and_cleanup():
    content = b"%PDF-1.4\npublic diagnostic fixture"
    with TemporaryFileServer(content, content_type="application/pdf") as server:
        with httpx.Client(trust_env=False) as client:
            response = client.get(server.url)
            assert response.content == content
            assert hashlib.sha256(response.content).hexdigest() == server.sha256
            assert response.headers["cache-control"] == "no-store"
            assert server.completed_gets == 1
            assert client.head(server.url).status_code == 200
            assert server.completed_gets == 1
            for path in ["/", "/.env", "/document.pdf", "/%2e%2e/.env", server.path + "?other=1"]:
                assert client.get(server.origin + path).status_code == 404
            assert client.post(server.url, content=b"replace").status_code == 501
        url = server.url
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        httpx.get(url, timeout=1, trust_env=False)


def test_expired_link_does_not_serve_bytes():
    with TemporaryFileServer(b"private bytes", content_type="image/png") as server:
        server.expires_at = time.monotonic() - 1
        response = httpx.get(server.url, trust_env=False)
        assert response.status_code == 410
        assert b"private bytes" not in response.content
        assert server.completed_gets == 0


@pytest.mark.parametrize("kwargs", [{"ttl_seconds": 0}, {"ttl_seconds": 1801}, {"content_type": "text/html"}])
def test_rejects_invalid_configuration(kwargs):
    args = {"content_type": "application/pdf", **kwargs}
    with pytest.raises(ValueError):
        TemporaryFileServer(b"data", **args)


def test_rejects_empty_and_oversize():
    for content in [b"", b"x" * (25 * 1024 * 1024 + 1)]:
        with pytest.raises(ValueError):
            TemporaryFileServer(content, content_type="application/pdf")


def test_rotation_and_revocation_never_serves_new_bytes_at_old_link():
    with TemporaryFileServer(b"first", content_type="application/pdf") as server:
        old = server.url
        assert httpx.get(old, trust_env=False).content == b"first"
        server.replace_document(b"second", "image/png")
        assert httpx.get(old, trust_env=False).status_code == 404
        response = httpx.get(server.url, trust_env=False)
        assert response.content == b"second" and response.headers["content-type"] == "image/png"
        server.revoke()
        assert httpx.get(server.url, trust_env=False).status_code == 410
