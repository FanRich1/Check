"""Private local-sample acceptance. Source documents and results never enter the delivery archive."""

import argparse
import hashlib
from pathlib import Path

from scripts.real_material_audit import audit_run
from shipping_billing.config import Settings
from shipping_billing.domain.contracts import NormalizationContext
from shipping_billing.harness.dataset import code_fingerprint
from shipping_billing.io import load_json, write_json
from shipping_billing.ocr.tunnel import PageOCR
from shipping_billing.parsers.local_text import LocalTextOCR
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import ReconciliationContext
from shipping_billing.web.service import native_path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["live", "replay"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--ocr-map", type=Path)
    parser.add_argument("--fixed-ocr", type=Path,
                        help="Live model-only repetition using exact real OCR archives, with no OCR network calls")
    args = parser.parse_args()
    if args.fixed_ocr and (args.mode != "live" or args.ocr_map):
        parser.error("--fixed-ocr requires live model mode and cannot be combined with --ocr-map")
    inventory = load_json(ROOT / "data/local/stage0/source-manifest.json")
    golden = load_json(ROOT / "data/local/stage0/golden-real.json")
    originals = {s["source_id"]: s for s in inventory["sources"] if s.get("parent_id") is None}
    for source in inventory["sources"]:
        if hashlib.sha256((ROOT / source["path"]).read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError("Private source hash mismatch: " + source["source_id"])
    settings = Settings(_env_file=ROOT / ".env") if args.mode == "live" else Settings()
    before = code_fingerprint()
    fixed_ocr = PageOCR(settings, storage=args.output / "fixed-ocr", mode="replay", mapping=args.fixed_ocr) if args.fixed_ocr else None
    run_pipeline(
        [ROOT / s["path"] for s in originals.values()],
        settings=settings,
        output=args.output,
        storage=native_path(args.storage),
        mode=args.mode,
        context=ReconciliationContext(
            normalization_context=NormalizationContext(accounting_entity="测试记账公司")
        ),
        cloudflared=ROOT / "outputs/stage2/tunnel/cloudflared.exe" if args.mode == "live" else None,
        ocr_map=args.ocr_map,
        ocr_provider=fixed_ocr,
        local_ocr=LocalTextOCR(),
        progress=lambda stage, count: print(f"Real sample check: {stage}, items={count}", flush=True),
    )
    report = audit_run(args.output, inventory, golden, storage=native_path(args.storage))
    report["code_sha256"] = before
    report["code_changed_during_run"] = code_fingerprint() != before
    report["passed"] = report["passed"] and not report["code_changed_during_run"]
    if fixed_ocr:
        report["ocr_input_mode"] = "fixed_real_response_replay_model_calls_are_live"
        report["ocr_network_calls"] = fixed_ocr.client.network_calls
    write_json(args.output / "acceptance.json", report)
    print({k: report[k] for k in ("passed", "expected_fees", "actual_fees", "expected_ocr_pages",
                                 "usable_ocr_pages", "page_archives_complete", "complete_online_success",
                                 "model_invocations", "ocr_network_calls", "code_changed_during_run")}, flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
