from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.extraction.client import ProbeCharge, StructuredModelClient
from shipping_billing.io import write_json
from shipping_billing.ocr.client import ADAPTER_VERSION, OCRClient, adapt, resolve_local_url

URL = "https://example.invalid/bill.pdf"
HASH = "a" * 64
PAYLOAD = {
    "success": True,
    "data": {
        "list": [
            {
                "invoiceNumber": "INV-1",
                "chargeList": [{"amount": 40, "currency": "USD"}],
                "page_index": [0, 1],
            }
        ]
    },
}


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"success": False, "data": {}}, "OCR_REJECTED_RESPONSE"),
        ({"success": True, "data": None}, "OCR_INVALID_STRUCTURE"),
        ({"success": True, "data": {"list": []}}, "OCR_EMPTY_CONTENT"),
        (
            {
                "success": True,
                "data": {
                    "createDate": "now",
                    "list": [
                        {"fileType": "NOTICE", "page_index": [0, 1], "chargeList": [], "invoiceNumber": ""}
                    ],
                },
            },
            "OCR_EMPTY_CONTENT",
        ),
        ({"success": True, "data": {"list": ["invalid"]}}, "OCR_INVALID_STRUCTURE"),
    ],
)
def test_ocr_rejects_nonusable_success(payload, code):
    with pytest.raises(BillingError) as e:
        adapt(payload)
    assert e.value.code == code


def test_vendor_indices_not_fabricated_as_pdf_pages():
    r = adapt(PAYLOAD)
    assert r["documents"][0]["vendor_page_indices"] == [0, 1]
    assert r["text_blocks"] == []
    assert r["evidence_kind"] == "vendor_structured_fields_not_original_text"


def test_cache_and_replay_do_not_call_network(tmp_path):
    calls = []

    def handle(request):
        import json

        calls.append(json.loads(request.content))
        return httpx.Response(200, json=PAYLOAD)

    client = OCRClient(Settings(), storage=tmp_path, transport=httpx.MockTransport(handle))
    first = client.recognize(URL, source_id="s", file_sha256=HASH, use_cache=True)
    assert calls == [{"file": URL, "task": "orderChange", "callBack": "", "taskType": ""}]
    second = client.recognize(URL, source_id="s", file_sha256=HASH, use_cache=True)
    third = client.recognize(
        URL, source_id="s", file_sha256=HASH, mode="replay", replay_path=Path(first["archive"])
    )
    assert len(calls) == 1
    assert [first["mode"], second["mode"], third["mode"]] == ["live", "cache", "replay"]
    assert second["network_calls"] == third["network_calls"] == 0


def test_read_timeout_never_reposts(tmp_path):
    calls = []

    def handle(req):
        calls.append(req)
        raise httpx.ReadTimeout("sensitive body")

    client = OCRClient(
        Settings(ocr_connect_retries=2), storage=tmp_path, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(BillingError) as e:
        client.recognize(URL, source_id="s")
    assert len(calls) == 1 and e.value.code == "OCR_TIMEOUT_UNKNOWN_OUTCOME"
    assert "sensitive body" not in str(e.value)


def test_only_connect_failure_can_retry(tmp_path):
    calls = []

    def handle(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ConnectError("connect")
        return httpx.Response(200, json=PAYLOAD)

    result = OCRClient(Settings(), storage=tmp_path, transport=httpx.MockTransport(handle)).recognize(
        URL, source_id="s"
    )
    assert result["network_calls"] == 2


@pytest.mark.parametrize(
    "status,body,code", [(500, PAYLOAD, "OCR_HTTP_FAILED"), (200, "not-json", "OCR_INVALID_JSON")]
)
def test_http_and_json_failure_archived_without_retry(tmp_path, status, body, code):
    calls = []

    def handle(req):
        calls.append(req)
        return (
            httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
        )

    with pytest.raises(BillingError) as e:
        OCRClient(Settings(), storage=tmp_path, transport=httpx.MockTransport(handle)).recognize(
            URL, source_id="s"
        )
    assert e.value.code == code and len(calls) == 1
    assert len(list((tmp_path / "raw").glob("*.json"))) == 1


def test_cache_needs_content_hash(tmp_path):
    with pytest.raises(BillingError) as e:
        OCRClient(Settings(), storage=tmp_path).recognize(URL, source_id="s", use_cache=True)
    assert e.value.code == "CACHE_REQUIRES_CONTENT_HASH"


@pytest.mark.parametrize("url", ["D:/bill.pdf", "file:///x.pdf", "https://user:secret@example.invalid/x"])
def test_local_path_is_not_remote_url(tmp_path, url):
    with pytest.raises(BillingError):
        OCRClient(Settings(), storage=tmp_path).recognize(url, source_id="s")


def test_replay_rejects_failed_http_status(tmp_path):
    path = tmp_path / "archive.json"
    write_json(
        path,
        {
            "source_id": "s",
            "file_sha256": HASH,
            "http_status": 500,
            "adapter_version": ADAPTER_VERSION,
            "task": "orderChange",
            "response": PAYLOAD,
        },
    )
    with pytest.raises(BillingError) as e:
        OCRClient(Settings(), storage=tmp_path).recognize(
            URL, source_id="s", file_sha256=HASH, mode="replay", replay_path=path
        )
    assert e.value.code == "INVALID_OCR_ARCHIVE"


def test_replay_needs_same_source_hash(tmp_path):
    path = tmp_path / "archive.json"
    write_json(
        path,
        {
            "source_id": "s",
            "file_sha256": HASH,
            "http_status": 200,
            "adapter_version": ADAPTER_VERSION,
            "task": "orderChange",
            "response": PAYLOAD,
        },
    )
    with pytest.raises(BillingError) as e:
        OCRClient(Settings(), storage=tmp_path).recognize(
            URL, source_id="s", file_sha256="b" * 64, mode="replay", replay_path=path
        )
    assert e.value.code == "REPLAY_HASH_MISMATCH"


def test_local_mapping_checks_exact_source_bytes(tmp_path):
    import hashlib

    local = tmp_path / "bill.pdf"
    local.write_bytes(b"not an actual PDF, hash identity fixture only")
    mapping = tmp_path / "mapping.json"
    digest = hashlib.sha256(local.read_bytes()).hexdigest()
    write_json(mapping, {"s": {"sha256": digest, "url": URL}})
    assert resolve_local_url(local, mapping, "s") == (URL, digest)
    local.write_bytes(b"changed")
    with pytest.raises(BillingError) as e:
        resolve_local_url(local, mapping, "s")
    assert e.value.code == "FILE_URL_MAPPING_MISMATCH"


@pytest.mark.parametrize(
    "content,code",
    [
        ("", "MODEL_EMPTY_CONTENT"),
        ("```json\n{}\n```", "MODEL_INVALID_STRUCTURE"),
        ('{"document_no":null,"amount":"40","currency":"USD"}', "MODEL_INVALID_STRUCTURE"),
        ('{"amount":1,"amount":2}', "MODEL_INVALID_STRUCTURE"),
    ],
)
def test_model_bad_json_not_repaired_or_accepted(content, code):
    runnable = RunnableLambda(lambda _: AIMessage(content=content))
    with pytest.raises(BillingError) as e:
        StructuredModelClient(Settings(), runnable=runnable).extract("some text", ProbeCharge)
    assert e.value.code == code


def test_langchain_injected_structured_number():
    runnable = RunnableLambda(
        lambda _: AIMessage(content='{"document_no":"I1","amount":9007199254740993.12,"currency":"USD"}')
    )
    r, meta = StructuredModelClient(Settings(), runnable=runnable).extract("test", ProbeCharge)
    assert str(r.amount) == "9007199254740993.12"
    assert meta["mode"] == "test_injected"


def test_model_does_not_call_network_when_offline():
    with pytest.raises(BillingError) as e:
        StructuredModelClient(Settings()).extract("test", ProbeCharge)
    assert e.value.code == "MODEL_NOT_CONFIGURED"


def test_whitespace_vendor_metadata_is_not_recognition():
    with pytest.raises(BillingError) as e:
        adapt({"success": True, "data": {"list": [{"invoiceNumber": "  ", "mblNumber": "None"}]}})
    assert e.value.code == "OCR_EMPTY_CONTENT"


def test_sdk_wire_debug_is_disabled():
    import logging

    from shipping_billing.network_logging import quiet_wire_logs

    logging.getLogger("openai").setLevel(logging.DEBUG)
    quiet_wire_logs()
    assert logging.getLogger("openai").level == logging.WARNING


def test_key_cannot_be_sent_to_an_unexpected_host():
    s = Settings(
        mode="online",
        model_provider="deepseek",
        model_name="deepseek-flash",
        model_api_key="CANARY",
        model_base_url="https://wrong.example.invalid",
    )
    with pytest.raises(BillingError) as e:
        StructuredModelClient(s).extract("test", ProbeCharge)
    assert e.value.code == "UNSUPPORTED_MODEL_ENDPOINT"
