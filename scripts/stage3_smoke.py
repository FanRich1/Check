"""Run parser acceptance on authored fixtures and optional local source inventory.

No expected values are supplied to an OCR provider. Labels are evaluated afterward.
"""

import argparse
from pathlib import Path

from shipping_billing.config import Settings
from shipping_billing.io import load_json, write_json
from shipping_billing.ocr.tunnel import PageOCR
from shipping_billing.parsers.engine import DocumentParser
from shipping_billing.parsers.local_text import LocalTextOCR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live", "replay"], default="offline")
    parser.add_argument("--cloudflared", type=Path)
    parser.add_argument("--ocr-map", type=Path)
    parser.add_argument("--local-ocr", action="store_true")
    parser.add_argument("--portable", action="store_true", help="Only synthetic files; no user's documents")
    parser.add_argument("--output", type=Path, default=Path("outputs/stage3"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    storage = root / "data/local/stage3"
    output = args.output.resolve()
    provider = None
    replay_map = {}
    if args.mode != "offline":
        actual_provider = PageOCR(
            Settings(), storage=storage, mode=args.mode, mapping=args.ocr_map, cloudflared=args.cloudflared
        )

        def provider(content, sid, page, content_type):
            print(f"OCR {sid} page {page}: starting", flush=True)
            result = actual_provider(content, sid, page, content_type)
            replay_map[result["page_source_id"]] = {
                "sha256": result["input_sha256"],
                "archive": result["archive"],
            }
            write_json(output / "replay-map.json", replay_map)
            print(f"OCR {sid} page {page}: {result['mode']}", flush=True)
            return result

    local = LocalTextOCR() if args.local_ocr else None
    engine = DocumentParser(storage=storage, mode=args.mode, ocr_provider=provider, local_ocr=local)
    inputs = []
    if not args.portable:
        manifest = load_json(root / "data/local/stage0/source-manifest.json")
        inputs += [
            (s["source_id"], root / s["path"]) for s in manifest["sources"] if s.get("parent_id") is None
        ]
    fixtures = root / "tests/fixtures/parsing"
    inputs += [
        ("synthetic-" + p.name.replace(".", "-"), p)
        for p in sorted(fixtures.iterdir())
        if p.suffix in (".doc", ".docx", ".xls", ".xlsx", ".jpg", ".tiff", ".pdf")
    ]
    try:
        for sid, path in inputs:
            print(f"Parsing {sid}", flush=True)
            source = engine.parse_file(path, sid)
            write_json(output / f"parse-{args.mode}.json", engine.report.model_dump(mode="python"))
            print(f"Result {sid}: {source.status}; fragments={len(source.fragments)}", flush=True)
    finally:
        if args.mode != "offline":
            actual_provider.close()
    if args.mode != "offline":
        source_ids = {s.source_id for s in engine.report.sources}
        for path in sorted(
            (storage / "ocr/raw").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            archive = load_json(path)
            sid = archive.get("source_id", "")
            if (
                sid.rsplit(":page-", 1)[0] in source_ids
                and sid not in replay_map
                and archive.get("http_status") == 200
            ):
                replay_map[sid] = {"sha256": archive["file_sha256"], "archive": str(path.resolve())}
        write_json(output / "replay-map.json", replay_map)
    print(f"Registered {len(engine.report.sources)} sources", flush=True)


if __name__ == "__main__":
    main()
