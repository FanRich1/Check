"""Audit private real runs without invoking OCR, a model, or the application ledger."""

import argparse
import hashlib
from collections import Counter
from pathlib import Path

from shipping_billing.io import load_json, write_json
from shipping_billing.ocr.client import validate_archive

ROOT = Path(__file__).resolve().parents[1]


def charge_key(charge):
    return charge["charge_item"], charge["amount"], charge.get("currency") or ""


def audit_run(folder, inventory, golden, *, storage=None):
    folder = Path(folder)
    manifest = load_json(folder / "manifest.json")
    parsed = load_json(folder / "parse.json")
    rows = load_json(folder / "records.json")
    mapping = load_json(folder / "ocr-replay-map.json")
    files = {f["source_id"]: f for f in manifest["files"]}
    originals = {s["source_id"]: s for s in inventory["sources"] if not s.get("parent_id")}
    expected = {}
    for case in golden["cases"]:
        sha = originals[case["source_ids"][0]]["sha256"]
        expected[sha] = case["fees"]

    def root(sid):
        while files[sid].get("parent_source_id"):
            sid = files[sid]["parent_source_id"]
        return files[sid]["sha256"]

    actual = {}
    for row in rows:
        roots = {root(sid) for sid in row["audit_trail"]["sources"] if sid in files}
        key = next(iter(roots)) if len(roots) == 1 else "AMBIGUOUS_SOURCE"
        actual.setdefault(key, []).append(row)
    differences, source_results = [], []
    for sha in sorted(set(expected) | set(actual)):
        wanted, found = expected.get(sha, []), actual.get(sha, [])
        left, right = Counter(charge_key(f["charge_details"]) for f in wanted), Counter(charge_key(f["charge_details"]) for f in found)
        missing = [dict(charge_item=k[0], amount=k[1], currency=k[2], count=count) for k, count in (left - right).items()]
        extra = [dict(charge_item=k[0], amount=k[1], currency=k[2], count=count) for k, count in (right - left).items()]
        if missing or extra:
            differences.append(dict(root_sha256=sha, missing=missing, extra=extra))
        source_results.append(dict(root_sha256=sha, expected=len(wanted), actual=len(found), matched=left == right))

    event_folder = folder / "work/events" if manifest["mode"] == "replay" else (
        Path(storage) / "parsing/events" if storage else None)
    events = [load_json(p) for p in event_folder.glob("*.json")] if event_folder else []
    coverage, archive_errors = [], []
    source_labels = {s["sha256"]: s for s in inventory["sources"]}
    seen_hashes = set()
    for source in parsed["sources"]:
        seen_hashes.add(source["sha256"])
        label = source_labels.get(source["sha256"], {})
        pages = label.get("pages", 1 if label.get("kind") == "image" else 0)
        metadata = source.get("metadata", {})
        statuses = {p["page"]: p for p in metadata.get("ocr_page_status", [])}
        for page in range(1, pages + 1):
            sid = f"{source['source_id']}:page-{page}"
            entry = mapping.get(sid)
            archive_valid = False
            archive_error = None
            if entry:
                path = Path(entry["archive"])
                path = path if path.is_absolute() else folder / path
                try:
                    saved = load_json(path)
                    validate_archive(saved)
                    archive_valid = saved.get("source_id") == sid and saved.get("file_sha256") == entry.get("sha256")
                    if not archive_valid:
                        archive_error = "identity_mismatch"
                except Exception as exc:
                    archive_error = getattr(exc, "code", type(exc).__name__)
            else:
                archive_error = "page_archive_missing"
            if archive_error:
                archive_errors.append(dict(source_id=sid, code=archive_error))
            calls = [e for e in events if e["source_id"] == sid]
            status = statuses.get(page, dict(status="not_run"))
            coverage.append(dict(source_id=sid, original_name=source["original_name"], page=page,
                                 status=status["status"], code=status.get("code"),
                                 archive_valid=archive_valid, event_count=len(calls),
                                 network_calls=sum(e.get("network_calls", 0) for e in calls),
                                 elapsed_seconds=sum(e.get("elapsed_seconds", 0) for e in calls),
                                 result_modes=[e.get("result_mode") for e in calls]))
    missing_sources = sorted(set(source_labels) - seen_hashes)
    expected_pages = sum(s.get("pages", 1 if s.get("kind") == "image" else 0) for s in inventory["sources"])
    issues = sorted({i["code"] for f in files.values() for i in f["issues"]})
    errors = [dict(source_id=f["source_id"], name=f["original_name"], stages=f["stages"],
                   issue_codes=sorted({i["code"] for i in f["issues"]})) for f in files.values() if f["status"] == "error"]
    online_coverage = (manifest["mode"] == "live" and len(coverage) == expected_pages and not missing_sources
                       and all(p["event_count"] == 1 and p["network_calls"] > 0 for p in coverage))
    all_pages_usable = len(coverage) == expected_pages and all(p["status"] == "completed" for p in coverage)
    fees_match = not differences and not missing_sources
    safe_review = all(r["operation"] == "PENDING_REVIEW" for r in rows)
    report = dict(mode=manifest["mode"], scope="private_assistant_reviewed_samples_not_independent_business_signoff",
                  input_documents=len(originals), sources=len(files), expected_fees=sum(len(f) for f in expected.values()),
                  actual_fees=len(rows), fee_name_amount_currency_and_root_match=fees_match,
                  all_unknown_roles_remain_pending=safe_review,
                  unexpected_update_hint="UPDATE_WITHOUT_OLD_SNAPSHOT" in issues,
                  model_invocations=manifest["model_invocations"], ocr_network_calls=manifest["ocr_network_calls"],
                  issue_codes=issues, status=manifest["status"], source_results=source_results,
                  differences=differences, execution_errors=errors, missing_sources=missing_sources,
                  purpose_confirmation_sources=manifest.get("counts", {}).get("purpose_pending_sources", 0),
                  processing_completed_without_technical_errors=not errors and not missing_sources,
                  expected_ocr_pages=expected_pages, page_coverage=coverage,
                  page_archives_complete=not archive_errors and len(coverage) == expected_pages and not missing_sources,
                  archive_errors=archive_errors, all_page_live_calls_recorded=online_coverage,
                  usable_ocr_pages=sum(p["status"] == "completed" for p in coverage),
                  complete_online_success=online_coverage and all_pages_usable and not errors,
                  independent_business_signoff=golden.get("independent_human_signoff") is True,
                  independent_accuracy_acceptance_complete=False,
                  known_ocr_limitations_retained=any(c.startswith("OCR_") for c in issues),
                  passed_scope="fee_preservation_and_safe_review_only_not_all_ocr_requests_successful")
    report["passed"] = fees_match and safe_review and not report["unexpected_update_hint"]
    return report


def compare_runs(first, second):
    def business(folder):
        rows = load_json(Path(folder) / "records.json")
        # Include every public business field and field evidence, excluding execution-only metadata.
        return [{k: r[k] for k in ("operation", "document_info", "transaction_party", "charge_details", "financial_attributes")}
                | {"field_evidence": r["audit_trail"]["field_evidence"]} for r in rows]
    from shipping_billing.io import dumps

    left, right = business(first), business(second)
    def plain(rows):
        return [{k: v for k, v in row.items() if k != "field_evidence"} for row in rows]
    return dict(business_and_evidence_identical=dumps(left) == dumps(right),
                business_identical=dumps(plain(left)) == dumps(plain(right)),
                evidence_identical=[r["field_evidence"] for r in left] == [r["field_evidence"] for r in right],
                first_sha256=hashlib.sha256(dumps(left).encode()).hexdigest(),
                second_sha256=hashlib.sha256(dumps(right).encode()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    report = audit_run(args.run, load_json(ROOT / "data/local/stage0/source-manifest.json"),
                       load_json(ROOT / "data/local/stage0/golden-real.json"), storage=args.storage)
    if args.compare:
        report["comparison"] = compare_runs(args.compare, args.run)
    write_json(args.output, report)
    print({k: report[k] for k in ("passed", "expected_fees", "actual_fees", "expected_ocr_pages",
                                 "usable_ocr_pages", "page_archives_complete", "complete_online_success")})
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
