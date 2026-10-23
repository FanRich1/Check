"""OCR diagnostic through a temporary single-file Cloudflare link.

Requires a separately downloaded official cloudflared binary. Uses an ephemeral
loopback port, a secret single-file route, and closes both processes on exit.
Defaults to the verified public Livingston sample. --local-file explicitly selects
one PDF/PNG/JPEG to expose temporarily to the OCR service via Cloudflare.
"""

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import httpx

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.ocr.client import OCRClient
from shipping_billing.ocr.file_bridge import TemporaryFileServer

PUBLIC_SHA256 = "5e61c43ca9c17a730eafe96de461244b956d0ce7aac6afb60d14719a5b01ada8"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloudflared", type=Path, required=True)
    parser.add_argument("--local-file", type=Path)
    parser.add_argument("--source-id", default="public-freight-tunnel")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", args.source_id):
        parser.error("source-id must contain only letters, digits, underscores or hyphens")
    if args.local_file and args.source_id == "public-freight-tunnel":
        parser.error("An explicit source-id is required with --local-file")
    root = Path(__file__).resolve().parents[1]
    output = root / "outputs/stage2/tunnel"
    if args.local_file:
        output = output / args.source_id
    output.mkdir(parents=True, exist_ok=True)
    source = args.local_file or root / "outputs/stage2/ocr-public-freight-sample.pdf"
    if source.stat().st_size > 25 * 1024 * 1024:
        parser.error("File exceeds 25 MiB")
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if not args.local_file and digest != PUBLIC_SHA256:
        raise SystemExit("Only the verified public diagnostic PDF may be served.")
    if content.startswith(b"%PDF-"):
        content_type = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        content_type = "image/jpeg"
    else:
        parser.error("Only PDF, PNG and JPEG file signatures are supported")
    result = {
        "public_sample_only": args.local_file is None,
        "source_id": args.source_id,
        "file_sha256": digest,
        "status": "starting",
        "read_timeout_seconds": 300,
    }
    process = None
    try:
        with TemporaryFileServer(content, content_type=content_type) as server:
            with (output / "cloudflared.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        str(args.cloudflared.resolve()),
                        "tunnel",
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
                    logs = (output / "cloudflared.log").read_text(encoding="utf-8", errors="replace")
                    found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", logs)
                    if found and "Registered tunnel connection" in logs:
                        hostname = found.group(0)
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.5)
                if hostname is None:
                    result["status"] = "tunnel_not_ready"
                    return
                result["tunnel_connected"] = True
                public_url = hostname + server.path
                with httpx.Client(timeout=30) as client:
                    response = client.get(public_url)
                result["public_download_status"] = response.status_code
                result["public_download_sha256_matches"] = (
                    hashlib.sha256(response.content).hexdigest() == digest
                )
                if response.status_code != 200 or not result["public_download_sha256_matches"]:
                    result["status"] = "public_download_failed"
                    return
                before = server.completed_gets
                print("Temporary link verified; submitting selected sample to OCR.", flush=True)
                start = time.monotonic()
                try:
                    result["recognition"] = OCRClient(
                        Settings(ocr_connect_retries=0), storage=root / "data/local/ocr"
                    ).recognize(
                        public_url,
                        source_id=args.source_id,
                        file_sha256=digest,
                    )
                    result["status"] = "usable"
                except BillingError as exc:
                    result["status"] = "ocr_failed"
                    result["error_code"] = exc.code
                finally:
                    result["ocr_duration_seconds"] = round(time.monotonic() - start, 2)
                    result["completed_file_gets_during_ocr"] = server.completed_gets - before
    except httpx.RequestError as exc:
        result["status"] = "network_failed"
        result["error_type"] = type(exc).__name__
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        result["tunnel_stopped"] = process is None or process.poll() is not None
        (output / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "recognition"}), flush=True)
        if result["status"] != "usable":
            raise SystemExit(3)


if __name__ == "__main__":
    main()
