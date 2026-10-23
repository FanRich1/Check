"""Explicit, short-lived single-document delivery for the specified OCR."""

import hashlib
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from shipping_billing.errors import BillingError
from shipping_billing.io import load_json
from shipping_billing.ocr.client import OCRClient, http_url
from shipping_billing.ocr.file_bridge import TemporaryFileServer


def verify_download(url, sha256, *, transport=None):
    """Retry only safe GET checks before an OCR request exists; never retry mismatched bytes."""
    with httpx.Client(timeout=httpx.Timeout(10, connect=5), transport=transport) as client:
        for attempt in range(3):
            try:
                response = client.get(url)
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise BillingError(
                        "TUNNEL_DOWNLOAD_FAILED", "Public file verification failed; OCR was not submitted."
                    ) from exc
            else:
                if response.status_code == 200:
                    if hashlib.sha256(response.content).hexdigest() != sha256:
                        raise BillingError(
                            "TUNNEL_CONTENT_MISMATCH", "Public download did not match the selected document."
                        )
                    return
                if response.status_code not in (404, 502, 503, 504) or attempt == 2:
                    raise BillingError(
                        "TUNNEL_DOWNLOAD_FAILED", "Public file was unavailable; OCR was not submitted."
                    )
            time.sleep(0.5 * (attempt + 1))


@contextmanager
def temporary_url(content, content_type, executable: Path, storage: Path):
    if not executable.is_file():
        raise BillingError("TUNNEL_TOOL_MISSING", "Explicit cloudflared executable was not found.")
    folder = storage / "tunnels" / uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / "config.yml"
    config.write_text("{}\n", encoding="utf-8")
    process = None
    try:
        with TemporaryFileServer(content, content_type=content_type, ttl_seconds=900) as server:
            logfile = folder / "tunnel.log"
            with logfile.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        str(executable.resolve()),
                        "tunnel",
                        "--config",
                        str(config.resolve()),
                        "--no-autoupdate",
                        "--protocol",
                        "http2",
                        "--url",
                        server.origin,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                deadline = time.monotonic() + 60
                hostname = None
                while time.monotonic() < deadline:
                    logs = logfile.read_text(encoding="utf-8", errors="replace")
                    found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", logs)
                    if found and "Registered tunnel connection" in logs:
                        hostname = found.group(0)
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.5)
                if hostname is None:
                    raise BillingError(
                        "TUNNEL_NOT_READY",
                        "Temporary file tunnel did not become ready; OCR was not submitted.",
                    )
                url = hostname + server.path
                verify_download(url, server.sha256)
                yield url, server
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


class PageOCR:
    """One source-page input per call; no inferred interpretation of vendor page_index."""

    def __init__(self, settings, *, storage, mode, mapping=None, cloudflared=None):
        self.client = OCRClient(settings, storage=storage / "ocr")
        self.storage = storage
        self.mode = mode
        self.mapping = load_json(mapping) if mapping else {}
        if not isinstance(self.mapping, dict):
            raise BillingError("INVALID_OCR_MAPPING", "OCR map must be an object keyed by page source ID.")
        self.mapping_parent = mapping.resolve().parent if mapping else None
        self.cloudflared = cloudflared
        self._context = None
        self._server = None
        self._base_url = None
        if mode == "live" and cloudflared is None and mapping is None:
            raise BillingError(
                "MISSING_OCR_DELIVERY", "Live parse requires explicit URL mapping or cloudflared path."
            )
        if mode == "replay" and mapping is None:
            raise BillingError("MISSING_OCR_REPLAY_MAP", "Replay parse requires a page archive map.")

    def close(self):
        if self._context is not None:
            self._context.__exit__(None, None, None)
            self._context = None
            self._server = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _deliver(self, content, content_type):
        if self._context is None:
            context = temporary_url(content, content_type, self.cloudflared, self.storage)
            url, server = context.__enter__()
            self._context = context
            self._server = server
            p = urlsplit(url)
            self._base_url = f"{p.scheme}://{p.netloc}"
            return url, server
        self._server.replace_document(content, content_type)
        url = self._base_url + self._server.path
        verify_download(url, self._server.sha256)
        return url, self._server

    def __call__(self, content, source_id, page, content_type):
        unit_id = f"{source_id}:page-{page}"
        digest = hashlib.sha256(content).hexdigest()
        if self.mode == "replay":
            entry = self.mapping.get(unit_id)
            if (
                not isinstance(entry, dict)
                or entry.get("sha256") != digest
                or not isinstance(entry.get("archive"), str)
            ):
                raise BillingError(
                    "OCR_REPLAY_PAGE_MISSING", "Replay map lacks the exact page content hash and archive."
                )
            archive = Path(entry["archive"])
            if not archive.is_absolute():
                archive = self.mapping_parent / archive
            result = self.client.recognize(
                "", source_id=unit_id, file_sha256=digest, mode="replay", replay_path=archive
            )
        elif self.mode == "live":
            cached = self.client.get_cached(unit_id, digest)
            if cached is not None:
                return {**cached, "input_sha256": digest, "page_source_id": unit_id}
            if self.cloudflared:
                try:
                    url, server = self._deliver(content, content_type)
                    before = server.completed_gets
                    result = self.client.recognize(url, source_id=unit_id, file_sha256=digest, use_cache=True)
                    result["delivery"] = {
                        "public_hash_verified": True,
                        "file_gets_during_ocr": server.completed_gets - before,
                    }
                finally:
                    if self._server is not None:
                        self._server.revoke()
                result["delivery"]["file_link_revoked"] = True
            else:
                entry = self.mapping.get(unit_id)
                if (
                    not isinstance(entry, dict)
                    or entry.get("sha256") != digest
                    or not isinstance(entry.get("url"), str)
                ):
                    raise BillingError(
                        "OCR_URL_PAGE_MISSING", "URL map lacks the exact page content hash and download URL."
                    )
                http_url(entry["url"])
                result = self.client.recognize(
                    entry["url"], source_id=unit_id, file_sha256=digest, use_cache=True
                )
        else:
            raise BillingError("INVALID_OCR_MODE", "Page OCR requires live or replay mode.")
        return {**result, "input_sha256": digest, "page_source_id": unit_id}
