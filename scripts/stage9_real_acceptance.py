"""Private local-sample acceptance. Source documents and results never enter the delivery archive."""

import argparse
from pathlib import Path

from shipping_billing.config import Settings
from shipping_billing.domain.contracts import NormalizationContext
from shipping_billing.io import load_json, write_json
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
    args = parser.parse_args()
    inventory = load_json(ROOT / "data/local/stage0/source-manifest.json")
    golden = load_json(ROOT / "data/local/stage0/golden-real.json")
    originals = {s["source_id"]: s for s in inventory["sources"] if s.get("parent_id") is None}
    expected = []
    for case in golden["cases"]:
        sha = originals[case["source_ids"][0]]["sha256"]
        expected.extend(
            (sha, f["charge_details"]["charge_item"], f["charge_details"]["amount"],
             f["charge_details"]["currency"] or "")
            for f in case["fees"]
        )
    settings = Settings(_env_file=ROOT / ".env") if args.mode == "live" else Settings()
    manifest = run_pipeline(
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
        local_ocr=LocalTextOCR(),
        progress=lambda stage, count: print(f"Real sample check: {stage}, items={count}", flush=True),
    )
    files = {f.source_id: f for f in manifest.files}

    def root(sid):
        while files[sid].parent_source_id:
            sid = files[sid].parent_source_id
        return sid

    actual = []
    rows = load_json(args.output / "records.json")
    for row in rows:
        roots = {root(sid) for sid in row["audit_trail"]["sources"] if sid in files}
        sha = files[next(iter(roots))].sha256 if len(roots) == 1 else "AMBIGUOUS_SOURCE"
        charge = row["charge_details"]
        actual.append((sha, charge["charge_item"], charge["amount"], charge["currency"] or ""))
    matched = sorted(actual) == sorted(expected)
    codes = sorted({i.code for f in manifest.files for i in f.issues})
    report = dict(
        mode=args.mode,
        scope="private_assistant_reviewed_samples_not_independent_business_signoff",
        input_documents=len(originals), sources=len(manifest.files), expected_fees=len(expected),
        actual_fees=len(rows), fee_name_amount_currency_and_root_match=matched,
        all_unknown_roles_remain_pending=all(r["operation"] == "PENDING_REVIEW" for r in rows),
        unexpected_update_hint="UPDATE_WITHOUT_OLD_SNAPSHOT" in codes,
        model_invocations=manifest.model_invocations, ocr_network_calls=manifest.ocr_network_calls,
        issue_codes=codes, status=manifest.status,
    )
    report["passed"] = matched and report["all_unknown_roles_remain_pending"] and not report[
        "unexpected_update_hint"
    ]
    report["known_ocr_limitations_retained"] = any(c.startswith("OCR_") for c in codes)
    report["complete_online_success"] = args.mode == "live" and manifest.status != "error"
    report["passed_scope"] = "fee_preservation_and_safe_review_only_not_all_ocr_requests_successful"
    write_json(args.output / "acceptance.json", report)
    print(report, flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
