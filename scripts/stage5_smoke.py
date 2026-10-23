"""Evaluate deterministic rule cases and optionally the private stage 4 real baseline."""

import argparse
from collections import Counter
from pathlib import Path

from stage5_cases import scenarios

from shipping_billing.domain.contracts import NormalizationReport
from shipping_billing.domain.engine import normalize
from shipping_billing.extraction.contracts import ExtractionReport
from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def compare(result, expected):
    line = result.lines[0]
    charge = line.candidate.charge_details
    actual = dict(
        name=charge.charge_item,
        amount=charge.amount,
        currency=charge.currency,
        payment=charge.payment_term,
        posting=line.financial_attributes.posting_type,
        ready=line.status == "ready",
        line_count=len(result.lines),
    )
    differences = {
        k: {"expected": v, "actual": actual.get(k)}
        for k, v in expected.items()
        if k not in ("blocked", "document_checks") and actual.get(k) != v
    }
    if expected.get("blocked") and not any(
        i.code == expected["blocked"] and i.blocks_automation for i in line.issues
    ):
        differences["blocked"] = expected["blocked"]
    if expected.get("document_checks") and not any(
        c.status == "passed" and c.code.startswith("DOCUMENT_") for c in result.document_checks
    ):
        differences["document_checks"] = "missing passing document check"
    if expected["ready"] and any(line.status != "ready" for line in result.lines):
        differences["all_lines_ready"] = False
    return differences


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--portable", action="store_true", help="Skip private real-sample comparisons")
    args = p.parse_args()
    checks = []
    schemas = NormalizationReport.model_json_schema()
    write_json(args.output / "schema.json", schemas)
    for case_id, source, context, expected in scenarios():
        result = normalize(source, context)
        equal = normalize(source, context) == result
        differences = compare(result, expected)
        if not equal:
            differences["deterministic_replay"] = False
        write_json(args.output / "cases" / (case_id + ".json"), result.model_dump(mode="python"))
        checks.append(
            {
                "case_id": case_id,
                "passed": not differences,
                "differences": differences,
                "expected": expected,
                "schema_valid": True,
                "repeat_consistent": equal,
            }
        )
    risk = [c for c in checks if c["expected"].get("ready") is False]
    finance = [c for c in checks if "posting" in c["expected"] and c["expected"]["posting"] is not None]
    ready = [c for c in checks if c["expected"].get("ready") is True]
    summary = {
        "scope": "deterministic_stage5_rules_not_end_to_end_or_model_accuracy",
        "rules": {"numerator": sum(c["passed"] for c in checks), "denominator": len(checks)},
        "category_counts": dict(Counter(c["case_id"].split("-")[0] for c in checks)),
        "clear_financial_directions": {
            "numerator": sum(c["passed"] for c in finance),
            "denominator": len(finance),
        },
        "risk_review": {"numerator": sum(c["passed"] for c in risk), "denominator": len(risk)},
        "clear_cases_ready": {"numerator": sum(c["passed"] for c in ready), "denominator": len(ready)},
        "checks": checks,
        "model_invocations": 0,
        "independent_human_signoff": False,
    }
    if not args.portable:
        extraction = ExtractionReport.model_validate(
            load_json(ROOT / "outputs/stage4/real-final-replay.json")
        )
        result = normalize(extraction)
        write_json(args.output / "real-normalized.json", result.model_dump(mode="python"))
        golden = load_json(ROOT / "data/local/stage0/golden-real.json")
        fee_checks = []
        for case in golden["cases"]:
            candidates = [line for line in result.lines if line.source_id == case["source_ids"][0]]
            for expected, line in zip(case["fees"], candidates):
                raw = expected["charge_details"]
                actual = line.candidate.charge_details
                correct = (
                    actual.amount == raw["amount"]
                    and actual.currency == raw["currency"]
                    and actual.charge_item == raw["charge_item"]
                )
                fee_checks.append(
                    {
                        "line_id": expected["line_id"],
                        "passed": correct,
                        "amount": actual.amount,
                        "currency": actual.currency,
                        "name": actual.charge_item,
                    }
                )
            if len(candidates) != len(case["fees"]):
                fee_checks.append(
                    {"passed": False, "error": "fee_count_mismatch", "source": case["source_ids"][0]}
                )
        sum_checks = [c for c in result.document_checks if c.code == "DOCUMENT_TOTAL"]
        summary["real"] = {
            "fees": {
                "numerator": sum(c["passed"] for c in fee_checks),
                "denominator": sum(len(c["fees"]) for c in golden["cases"]),
            },
            "fee_checks": fee_checks,
            "source_count": len(result.files),
            "line_count": len(result.lines),
            "total_checks_passed": sum(c.status == "passed" for c in sum_checks),
            "all_pending_review_without_entity": all(
                line.status == "pending_review" and line.financial_attributes.posting_type is None
                for line in result.lines
            ),
            "totals": [c.model_dump(mode="python") for c in sum_checks],
            "passed": all(c["passed"] for c in fee_checks)
            and len(result.lines) == 7
            and len(sum_checks) == 3
            and all(c.status == "passed" for c in sum_checks),
        }
    summary["passed"] = all(c["passed"] for c in checks) and summary.get("real", {"passed": True})["passed"]
    write_json(args.output / "evaluation.json", summary)
    print(
        {
            k: summary[k]
            for k in ("rules", "clear_financial_directions", "risk_review", "clear_cases_ready", "passed")
        }
    )
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
