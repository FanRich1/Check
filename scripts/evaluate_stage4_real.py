"""Score real extraction against the frozen stage 0 assistant visual baseline."""

import argparse
from pathlib import Path

from shipping_billing.extraction.contracts import ExtractionReport
from shipping_billing.extraction.engine import contains_value
from shipping_billing.extraction.evaluation import compact, score
from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise SystemExit("Output must not overwrite input")
    report = ExtractionReport.model_validate(load_json(args.input))
    golden = load_json(ROOT / "data/local/stage0/golden-real.json")
    expected, reports = [], {}
    for case in golden["cases"]:
        sid = case["source_ids"][0]
        units = [u for u in report.evidence_units if u.source_id == sid]
        available = {u.unit_id for u in units}
        fees = []
        for fee in case["fees"]:
            charge = fee["charge_details"]
            # Identify physical source rows from the baseline name AND amount in original evidence,
            # before looking at predictions. Matching then uses the evidence location, not amount.
            unit = next(
                (
                    u
                    for u in units
                    if u.unit_id in available
                    and compact(charge["charge_item_raw"]) in compact(u.text)
                    and contains_value(charge["amount"], u.text)
                ),
                None,
            )
            if unit:
                available.remove(unit.unit_id)
            fees.append(
                {
                    "source_id": sid,
                    "row_unit": unit.unit_id if unit else "MISSING_BASELINE_ROW",
                    "name": charge["charge_item_raw"],
                    "amount": charge["amount"],
                    "currency": charge["currency"],
                    "reference_no": case["document_info"]["reference_no"],
                    **case["transaction_party"],
                }
            )
        expected.append({"case_id": case["case_id"], "fees": fees})
        files = [f for f in report.files if f.source_id == sid or f.source_id.startswith(sid + "/")]
        reports[case["case_id"]] = report.model_copy(update={"files": files})
    result = score(expected, reports)
    expected_rows = sum(len(case["fees"]) for case in expected)
    result["all_real_fee_rows_retained"] = (
        result["recall"]["numerator"] == expected_rows and result["precision"]["denominator"] == expected_rows
    )
    result["baseline"] = "stage0_assistant_visual_baseline_not_independent_human_signoff"
    result["passed"] = result["passed"] and result["all_real_fee_rows_retained"]
    write_json(args.output, result)
    print(
        {k: result[k] for k in ("precision", "recall", "key_field_accuracy", "evidence_locatable", "passed")}
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
