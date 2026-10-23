"""Minimal ordered comparison of independently supplied predictions.

No extraction, OCR, matching, snapshot mutation, or claim of end-to-end accuracy.
"""

from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from shipping_billing.errors import BillingError
from shipping_billing.io import load_json
from shipping_billing.models import BillingRecord, BillingRequest

RECORDS = TypeAdapter(list[BillingRecord])


def project(record: BillingRecord) -> dict[str, Any]:
    value = record.model_dump(mode="python")
    # New technical IDs depend on a later identity algorithm. Matched IDs are contractual.
    if value["existing_record_id"] is None:
        value.pop("record_id")
    audit = value.pop("audit_trail")
    value["issue_codes"] = sorted(i["code"] for i in audit["issues"])
    value["changes"] = [{k: c[k] for k in ("path", "old", "new")} for c in audit["changes"]]
    return value


def evaluate(case: Path, actual_path: Path) -> dict[str, Any]:
    expected_path = case / "expected.json"
    # Prevent passing the expectation file itself as an alleged independent prediction.
    if actual_path.resolve() == expected_path.resolve():
        raise BillingError("PREDICTION_IS_LABEL", "Provide a separate JSON prediction array, not labels.")
    BillingRequest.model_validate(load_json(case / "request.json"))
    labels = load_json(expected_path)
    if not isinstance(labels, dict) or not isinstance(labels.get("expected_records"), list):
        raise BillingError("INVALID_LABELS", "Case must contain a labelled expected_records array.")
    expected = RECORDS.validate_python(labels["expected_records"])
    raw_actual = load_json(actual_path)
    try:
        actual = RECORDS.validate_python(raw_actual)
    except ValidationError as exc:
        actual_count = len(raw_actual) if isinstance(raw_actual, list) else None
        denominator = max(len(expected), actual_count or 0)
        return {
            "schema_version": "0.1.0",
            "scope": "offline_supplied_prediction_comparison",
            "alignment": "ordered_rows; full provenance-based alignment arrives in later stages",
            "pipeline_executed": False,
            "snapshot_mutation_evaluated": False,
            "expected_count": len(expected),
            "actual_count": actual_count,
            "exact_business_records": {
                "numerator": 0,
                "denominator": denominator,
                "rate": 0 if denominator else None,
            },
            "passed": False,
            "prediction_contract_valid": False,
            "differences": [{"kind": "invalid_prediction_contract", "error_count": exc.error_count()}],
        }
    differences = []
    exact = 0
    for index in range(max(len(expected), len(actual))):
        if index >= len(actual):
            differences.append({"row": index, "kind": "missing_record"})
        elif index >= len(expected):
            differences.append({"row": index, "kind": "extra_record"})
        elif project(expected[index]) == project(actual[index]):
            exact += 1
        else:
            expected_view, actual_view = project(expected[index]), project(actual[index])
            differences.append(
                {
                    "row": index,
                    "kind": "business_difference",
                    "different_groups": [k for k in expected_view if expected_view[k] != actual_view.get(k)],
                }
            )
    denominator = max(len(expected), len(actual))
    return {
        "schema_version": "0.1.0",
        "scope": "offline_supplied_prediction_comparison",
        "alignment": "ordered_rows; full provenance-based alignment arrives in later stages",
        "pipeline_executed": False,
        "snapshot_mutation_evaluated": False,
        "expected_count": len(expected),
        "actual_count": len(actual),
        "exact_business_records": {
            "numerator": exact,
            "denominator": denominator,
            "rate": exact / denominator if denominator else None,
        },
        "passed": not differences,
        "prediction_contract_valid": True,
        "differences": differences,
    }
