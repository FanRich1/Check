"""Synchronous specified OCR with conservative retries and explicit cache/replay."""

import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.network_logging import quiet_wire_logs

ADAPTER_VERSION = "order-change-v1"


def resolve_local_url(local_file: Path, mapping_file: Path, source_id: str):
    try:
        digest = hashlib.sha256(local_file.read_bytes()).hexdigest()
    except OSError as exc:
        raise BillingError("LOCAL_SOURCE_UNREADABLE", "Cannot read the local source file.") from exc
    mapping = load_json(mapping_file)
    entry = mapping.get(source_id) if isinstance(mapping, dict) else None
    if not isinstance(entry, dict) or entry.get("sha256") != digest:
        raise BillingError(
            "FILE_URL_MAPPING_MISMATCH", "URL mapping must identify the exact local content hash."
        )
    url = entry.get("url")
    if not isinstance(url, str):
        raise BillingError("INVALID_OCR_FILE_URL", "Mapped URL must be an HTTP(S) string.")
    http_url(url)
    return url, digest


def validate_archive(archive):
    if (
        not isinstance(archive, dict)
        or archive.get("http_status") != 200
        or archive.get("adapter_version") != ADAPTER_VERSION
        or archive.get("task") != "orderChange"
    ):
        raise BillingError(
            "INVALID_OCR_ARCHIVE", "Archive must be a successful versioned orderChange response."
        )


def http_url(value: str):
    try:
        p = urlsplit(value)
    except ValueError as exc:
        raise BillingError("INVALID_OCR_FILE_URL", "OCR file URL is malformed.") from exc
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise BillingError(
            "INVALID_OCR_FILE_URL", "OCR requires a downloadable HTTP(S) URL, not a local path."
        )


def adapt(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise BillingError("OCR_REJECTED_RESPONSE", "OCR response must explicitly report success.")
    data = payload.get("data")
    if not isinstance(data, (dict, list)):
        raise BillingError("OCR_INVALID_STRUCTURE", "OCR data must be an object or array.")
    texts = []
    documents = []
    # Raw-text alternatives are supported conservatively, not claimed as observed live schema.
    if isinstance(data, dict):
        for key in ["text", "markdown", "content"]:
            if isinstance(data.get(key), str) and data[key].strip():
                texts.append(data[key])
        rows = data.get("list", [])
    else:
        rows = data
    if not isinstance(rows, list):
        raise BillingError("OCR_INVALID_STRUCTURE", "OCR document list must be an array.")
    for row in rows:
        if not isinstance(row, dict):
            raise BillingError("OCR_INVALID_STRUCTURE", "OCR document must be an object.")
        for key in ["text", "markdown", "content"]:
            if isinstance(row.get(key), str) and row[key].strip():
                texts.append(row[key])
        for key in ("chargeList", "ctrList", "other"):
            if key in row and not isinstance(row[key], list):
                raise BillingError("OCR_INVALID_STRUCTURE", "Vendor list fields must be arrays.")
        facts = {
            # Preserve structured facts as vendor output, not invented original text.
            k: row[k]
            for k in [
                "invoiceNumber",
                "mblNumber",
                "shipper",
                "consignee",
                "remark",
                "chargeList",
                "ctrList",
                "other",
            ]
            if k in row
            and row[k] not in (None, "", [], {})
            and (not isinstance(row[k], str) or row[k].strip().lower() not in ("", "none", "null", "n/a"))
        }
        if facts:
            documents.append({"vendor_facts": facts, "vendor_page_indices": row.get("page_index")})
    if not texts and not documents:
        raise BillingError(
            "OCR_EMPTY_CONTENT",
            "OCR returned only an empty business template, not usable recognition content.",
        )
    return {
        "adapter_version": ADAPTER_VERSION,
        "text_blocks": texts,
        "documents": documents,
        "evidence_kind": "vendor_structured_fields_not_original_text" if documents else "vendor_text",
    }


class OCRClient:
    def __init__(self, settings: Settings, *, storage: Path, transport=None):
        self.settings = settings
        self.storage = storage
        self.transport = transport
        self.network_calls = 0
        self.last_archive = None

    def cache_path(self, source_id, file_sha256):
        fingerprint = hashlib.sha256(
            dumps(
                {
                    "file_sha256": file_sha256,
                    "source_id": source_id,
                    "endpoint": self.settings.ocr_endpoint,
                    "task": "orderChange",
                    "adapter_version": ADAPTER_VERSION,
                }
            ).encode()
        ).hexdigest()
        return self.storage / "cache" / f"{fingerprint}.json"

    def get_cached(self, source_id, file_sha256):
        if not isinstance(file_sha256, str) or not re.fullmatch("[0-9a-f]{64}", file_sha256):
            raise BillingError("INVALID_CONTENT_HASH", "Cache lookup requires a lowercase SHA-256 digest.")
        cache = self.cache_path(source_id, file_sha256)
        if not cache.is_file():
            return None
        saved = load_json(cache)
        validate_archive(saved)
        if saved.get("file_sha256") != file_sha256 or saved.get("source_id") != source_id:
            raise BillingError("OCR_CACHE_IDENTITY_MISMATCH", "Cached source identity is invalid.")
        return {
            **adapt(saved.get("response")),
            "mode": "cache",
            "source_id": source_id,
            "network_calls": 0,
            "archive": str(cache.resolve()),
        }

    def recognize(
        self,
        file_url: str,
        *,
        source_id: str,
        file_sha256: str | None = None,
        mode: str = "live",
        replay_path: Path | None = None,
        use_cache: bool = False,
    ):
        quiet_wire_logs()
        self.last_archive = None
        if file_sha256 is not None and not re.fullmatch("[0-9a-f]{64}", file_sha256):
            raise BillingError("INVALID_CONTENT_HASH", "Content hash must be a lowercase SHA-256 digest.")
        if mode not in ("live", "replay"):
            raise BillingError("INVALID_OCR_MODE", "Use live or explicit replay mode.")
        if mode == "replay":
            if replay_path is None:
                raise BillingError("MISSING_REPLAY_FILE", "Explicit replay requires a response archive.")
            archive = load_json(replay_path)
            validate_archive(archive)
            if not isinstance(archive, dict) or archive.get("source_id") != source_id:
                raise BillingError("REPLAY_SOURCE_MISMATCH", "Replay source identity does not match.")
            if file_sha256 is None or archive.get("file_sha256") != file_sha256:
                raise BillingError("REPLAY_HASH_MISMATCH", "Replay requires the original content hash.")
            self.last_archive = replay_path
            return {
                **adapt(archive.get("response")),
                "mode": "replay",
                "source_id": source_id,
                "network_calls": 0,
                "archive": str(replay_path),
            }
        http_url(file_url)
        if file_sha256 is not None and not re.fullmatch("[0-9a-f]{64}", file_sha256):
            raise BillingError("INVALID_CONTENT_HASH", "Content hash must be a lowercase SHA-256 digest.")
        if use_cache and file_sha256 is None:
            raise BillingError(
                "CACHE_REQUIRES_CONTENT_HASH", "URL alone cannot establish immutable file identity."
            )
        s = self.settings
        request = {"file": file_url, "task": "orderChange", "callBack": "", "taskType": ""}
        cache = self.cache_path(source_id, file_sha256)
        if use_cache:
            cached = self.get_cached(source_id, file_sha256)
            if cached is not None:
                return cached
        attempts = 0
        while True:
            attempts += 1
            try:
                with httpx.Client(
                    timeout=httpx.Timeout(s.ocr_read_timeout, connect=s.ocr_connect_timeout),
                    transport=self.transport,
                    follow_redirects=False,
                ) as client:
                    self.network_calls += 1
                    response = client.post(s.ocr_endpoint, json=request)
                break
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                if attempts > s.ocr_connect_retries:
                    raise BillingError(
                        "OCR_CONNECT_FAILED", "OCR connection failed after bounded connect attempts."
                    ) from exc
            except httpx.TimeoutException as exc:
                raise BillingError(
                    "OCR_TIMEOUT_UNKNOWN_OUTCOME", "OCR timed out after submission; no automatic repost."
                ) from exc
            except httpx.RequestError as exc:
                raise BillingError(
                    "OCR_TRANSPORT_FAILED", "OCR transport failed; no automatic repost."
                ) from exc
        archive_path = self.storage / "raw" / f"{uuid4().hex}.json"
        try:
            payload = response.json()
        except ValueError as exc:
            write_json(
                archive_path,
                {
                    "source_id": source_id,
                    "file_sha256": file_sha256,
                    "http_status": response.status_code,
                    "response_text": response.text,
                },
            )
            self.last_archive = archive_path
            raise BillingError(
                "OCR_INVALID_JSON", "OCR did not return JSON; response archived locally."
            ) from exc
        saved = {
            "source_id": source_id,
            "file_sha256": file_sha256,
            "http_status": response.status_code,
            "response": payload,
            "adapter_version": ADAPTER_VERSION,
            "task": "orderChange",
            "endpoint": s.ocr_endpoint,
        }
        write_json(archive_path, saved)
        self.last_archive = archive_path
        if response.status_code != 200:
            raise BillingError(
                "OCR_HTTP_FAILED", "OCR returned an unsuccessful HTTP status; no automatic repost."
            )
        result = adapt(payload)
        if use_cache:
            write_json(cache, saved)
        return {
            **result,
            "mode": "live",
            "source_id": source_id,
            "network_calls": attempts,
            "archive": str(archive_path),
        }
