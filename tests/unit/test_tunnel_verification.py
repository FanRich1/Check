import hashlib

import httpx
import pytest

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.ocr.file_bridge import TemporaryFileServer
from shipping_billing.ocr.tunnel import PageOCR, verify_download

URL = "https://example.invalid/capability/document.pdf"
CONTENT = b"explicitly selected document"
SHA = hashlib.sha256(CONTENT).hexdigest()


def test_pre_submission_connection_failure_can_recover_without_posting(monkeypatch):
    calls = []
    monkeypatch.setattr("shipping_billing.ocr.tunnel.time.sleep", lambda _: None)

    def handle(request):
        calls.append(request.method)
        if len(calls) < 3:
            raise httpx.ConnectError("not ready", request=request)
        return httpx.Response(200, content=CONTENT)

    verify_download(URL, SHA, transport=httpx.MockTransport(handle))
    assert calls == ["GET"] * 3


def test_failed_pre_submission_checks_are_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr("shipping_billing.ocr.tunnel.time.sleep", lambda _: None)

    def handle(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("not ready", request=request)

    with pytest.raises(BillingError) as exc:
        verify_download(URL, SHA, transport=httpx.MockTransport(handle))
    assert exc.value.code == "TUNNEL_DOWNLOAD_FAILED"
    assert calls == ["GET"] * 3


@pytest.mark.parametrize("status", [404, 502, 503, 504])
def test_edge_propagation_status_can_recover(monkeypatch, status):
    calls = []
    monkeypatch.setattr("shipping_billing.ocr.tunnel.time.sleep", lambda _: None)

    def handle(request):
        calls.append(request.method)
        return httpx.Response(status if len(calls) == 1 else 200, content=CONTENT)

    verify_download(URL, SHA, transport=httpx.MockTransport(handle))
    assert calls == ["GET"] * 2


@pytest.mark.parametrize("status,content,code", [
    (200, b"different bytes", "TUNNEL_CONTENT_MISMATCH"),
    (403, CONTENT, "TUNNEL_DOWNLOAD_FAILED"),
])
def test_wrong_content_and_permission_denial_never_retry(monkeypatch, status, content, code):
    calls = []
    monkeypatch.setattr("shipping_billing.ocr.tunnel.time.sleep", lambda _: pytest.fail("Unexpected retry"))

    def handle(request):
        calls.append(request.method)
        return httpx.Response(status, content=content)

    with pytest.raises(BillingError) as exc:
        verify_download(URL, SHA, transport=httpx.MockTransport(handle))
    assert exc.value.code == code and calls == ["GET"]


def test_reused_tunnel_verifies_the_new_page_bytes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("shipping_billing.ocr.tunnel.verify_download", lambda url, sha: calls.append((url, sha)))
    with TemporaryFileServer(CONTENT, content_type="application/pdf") as server:
        old_path = server.path
        provider = PageOCR(Settings(), storage=tmp_path, mode="live", cloudflared=tmp_path / "fake")
        provider._context = object()
        provider._server = server
        provider._base_url = server.origin
        new = b"a different selected page"
        url, same_server = provider._deliver(new, "application/pdf")
        assert same_server is server and server.path != old_path
        assert calls == [(url, hashlib.sha256(new).hexdigest())]
