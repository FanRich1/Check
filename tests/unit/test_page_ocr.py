import hashlib

import pytest

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import write_json
from shipping_billing.ocr.client import ADAPTER_VERSION
from shipping_billing.ocr.tunnel import PageOCR


def test_replay_page_identity_and_changed_content(tmp_path):
    data = b"page-bytes"
    digest = hashlib.sha256(data).hexdigest()
    archive = tmp_path / "archive.json"
    write_json(
        archive,
        {
            "source_id": "input:page-2",
            "file_sha256": digest,
            "http_status": 200,
            "adapter_version": ADAPTER_VERSION,
            "task": "orderChange",
            "response": {"success": True, "data": {"text": "invoice"}},
        },
    )
    mapping = tmp_path / "map.json"
    write_json(mapping, {"input:page-2": {"sha256": digest, "archive": "archive.json"}})
    provider = PageOCR(Settings(), storage=tmp_path, mode="replay", mapping=mapping)
    result = provider(data, "input", 2, "application/pdf")
    assert result["mode"] == "replay" and result["network_calls"] == 0
    assert result["text_blocks"] == ["invoice"]
    with pytest.raises(BillingError, match="exact page"):
        provider(data, "input", 1, "application/pdf")
    with pytest.raises(BillingError, match="exact page"):
        provider(b"changed", "input", 2, "application/pdf")


def test_live_does_not_upload_without_explicit_delivery(tmp_path):
    with pytest.raises(BillingError) as exc:
        PageOCR(Settings(), storage=tmp_path, mode="live")
    assert exc.value.code == "MISSING_OCR_DELIVERY"


@pytest.mark.parametrize("failure,deliveries,posts", [
    ("TUNNEL_NOT_READY", 2, 1), ("TUNNEL_DOWNLOAD_FAILED", 2, 1),
    ("TUNNEL_CONTENT_MISMATCH", 1, 0), ("OCR_TIMEOUT_UNKNOWN_OUTCOME", 1, 1),
])
def test_retry_only_before_ocr_submission(tmp_path, monkeypatch, failure, deliveries, posts):
    from types import SimpleNamespace

    provider = PageOCR(Settings(), storage=tmp_path, mode="live", cloudflared=tmp_path / "fake")
    calls = {"deliver": 0, "post": 0}
    server = SimpleNamespace(completed_gets=0, revoke=lambda: None)

    def deliver(*args):
        calls["deliver"] += 1
        if calls["deliver"] == 1 and failure.startswith("TUNNEL_"):
            raise BillingError(failure, "delivery failure")
        return "https://example.test/page", server

    def recognize(*args, **kwargs):
        calls["post"] += 1
        if failure == "OCR_TIMEOUT_UNKNOWN_OUTCOME":
            raise BillingError(failure, "unknown outcome")
        return {"text_blocks": ["verified"]}

    monkeypatch.setattr(provider, "_deliver", deliver)
    monkeypatch.setattr(provider.client, "get_cached", lambda *args: None)
    monkeypatch.setattr(provider.client, "recognize", recognize)
    if failure in ("TUNNEL_CONTENT_MISMATCH", "OCR_TIMEOUT_UNKNOWN_OUTCOME"):
        with pytest.raises(BillingError) as exc:
            provider(b"page", "source", 1, "application/pdf")
        assert exc.value.code == failure
    else:
        provider(b"page", "source", 1, "application/pdf")
    assert calls == {"deliver": deliveries, "post": posts}


def test_batch_reuses_tunnel_revokes_each_link_and_cache_needs_no_tunnel(tmp_path, monkeypatch):
    import json
    from contextlib import contextmanager

    import httpx

    import shipping_billing.ocr.tunnel as module
    from shipping_billing.ocr.client import OCRClient
    from shipping_billing.ocr.file_bridge import TemporaryFileServer

    tunnels = []
    downloads = []

    @contextmanager
    def local_tunnel(content, content_type, *args):
        with TemporaryFileServer(content, content_type=content_type) as server:
            tunnels.append(server)
            yield server.url, server

    def handler(request):
        url = json.loads(request.content)["file"]
        response = httpx.get(url, trust_env=False)
        assert response.status_code == 200
        downloads.append(response.content)
        return httpx.Response(200, json={"success": True, "data": {"text": "recognized"}})

    monkeypatch.setattr(module, "temporary_url", local_tunnel)
    with PageOCR(Settings(), storage=tmp_path, mode="live", cloudflared=tmp_path / "fake") as provider:
        provider.client = OCRClient(
            Settings(), storage=tmp_path / "ocr", transport=httpx.MockTransport(handler)
        )
        assert provider(b"first", "source", 1, "application/pdf")["mode"] == "live"
        assert httpx.get(tunnels[0].url, trust_env=False).status_code == 410
        assert provider(b"second", "source", 2, "image/png")["mode"] == "live"
        assert len(tunnels) == 1 and downloads == [b"first", b"second"]
        assert provider(b"first", "source", 1, "application/pdf")["mode"] == "cache"
        assert len(downloads) == 2
    assert tunnels[0]._server is None


def test_empty_response_is_archived_mapped_and_replayed_without_another_post(tmp_path):
    import httpx

    content = b"non-billing-image"
    digest = hashlib.sha256(content).hexdigest()
    mapping = tmp_path / "urls.json"
    write_json(mapping, {"image:page-1": dict(sha256=digest, url="https://example.invalid/banner.png")})
    provider = PageOCR(Settings(), storage=tmp_path / "live", mode="live", mapping=mapping)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": {"list": []}})

    provider.client.transport = httpx.MockTransport(handle)
    with pytest.raises(BillingError) as exc:
        provider(content, "image", 1, "image/png")
    assert exc.value.code == "OCR_EMPTY_CONTENT"
    assert len(requests) == provider.client.network_calls == 1
    assert provider.events[0]["network_calls"] == 1
    assert provider.events[0]["code"] == "OCR_EMPTY_CONTENT"
    assert provider.events[0]["archive_sha256"]
    replay_map = tmp_path / "replay.json"
    write_json(replay_map, provider.replay_map)
    replay = PageOCR(Settings(), storage=tmp_path / "replay", mode="replay", mapping=replay_map)
    with pytest.raises(BillingError) as again:
        replay(content, "image", 1, "image/png")
    assert again.value.code == "OCR_EMPTY_CONTENT"
    assert replay.events[0]["network_calls"] == 0
    assert replay.events[0]["archive_sha256"] == provider.events[0]["archive_sha256"]


def test_timeout_journal_counts_submission_and_never_creates_a_replay_success(tmp_path):
    import httpx

    content = b"page"
    mapping = tmp_path / "urls.json"
    write_json(mapping, {"pdf:page-1": dict(sha256=hashlib.sha256(content).hexdigest(),
                                          url="https://example.invalid/page.pdf")})
    provider = PageOCR(Settings(), storage=tmp_path, mode="live", mapping=mapping)

    def timeout(request):
        raise httpx.ReadTimeout("unknown outcome", request=request)

    provider.client.transport = httpx.MockTransport(timeout)
    with pytest.raises(BillingError) as exc:
        provider(content, "pdf", 1, "application/pdf")
    assert exc.value.code == "OCR_TIMEOUT_UNKNOWN_OUTCOME"
    assert provider.client.network_calls == 1
    assert provider.events[0]["outcome_unknown"] is True
    assert provider.events[0]["network_calls"] == 1
    assert not provider.replay_map
