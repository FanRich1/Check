"""Reproducible stage 6 component acceptance, with explicit expected decisions."""

import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.stage6_cases import scenarios
from shipping_billing.domain.contracts import NormalizationContext, NormalizationReport
from shipping_billing.domain.engine import normalize
from shipping_billing.extraction.contracts import ExtractionReport
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationReport
from shipping_billing.reconciliation.engine import reconcile

ROOT = Path(__file__).resolve().parents[1]


def check_case(case, output):
    cid, source, context, state, expected = deepcopy(case)
    before = state.model_dump(mode="python")
    normalized = normalize(source, context.normalization_context)
    result = reconcile(normalized, source, context=context, state=state)
    actual = dict(
        operations=[d.operation for d in result.decisions],
        paths=sorted(c.path for r in result.records for c in r.audit_trail.changes),
    )
    differences = {}
    if actual["paths"] != sorted(expected["paths"]):
        differences["paths"] = dict(expected=expected["paths"], actual=actual["paths"])
    if actual["operations"] != expected["operations"]:
        differences["operations"] = dict(expected=expected["operations"], actual=actual["operations"])
    if expected.get("target") and result.decisions[0].record_id != expected["target"]:
        differences["target"] = result.decisions[0].record_id
    if expected.get("rule") and result.decisions[0].match_rule != expected["rule"]:
        differences["rule"] = result.decisions[0].match_rule
    if expected.get("issue") and not any(
        i.code == expected["issue"] for d in result.decisions for i in d.issues
    ):
        differences["issue"] = expected["issue"]
    path = output / "cases" / (cid + ".json")
    write_json(path, result.model_dump(mode="python"))
    decoded = load_json(path)
    Draft202012Validator(ReconciliationReport.model_json_schema()).validate(decoded)
    restored = LedgerState.model_validate(decoded["next_state"])
    repeat = reconcile(normalized, source, context=context, state=restored)
    idempotent = repeat.next_state == restored and all(
        d.operation not in ("CREATE", "UPDATE") for d in repeat.decisions
    )
    if not idempotent:
        differences["idempotence"] = False
    if before != state.model_dump(mode="python"):
        differences["mutated_input"] = True
    if cid in ("new-create", "authorized-amount", "reference-exact", "unauthorized-amount"):
        folder = output / "examples" / cid
        for name, value in [
            ("extraction", source),
            ("normalized", normalized),
            ("context", context),
            ("old-state", state),
            ("result", result),
        ]:
            write_json(folder / (name + ".json"), value.model_dump(mode="python"))
        write_json(
            folder / "records.json", [r.model_dump(mode="python", exclude_unset=True) for r in result.records]
        )
        write_json(folder / "next-state.json", result.next_state.model_dump(mode="python"))
    return dict(
        case_id=cid,
        passed=not differences,
        expected=expected,
        actual=actual,
        differences=differences,
        idempotent=idempotent,
        schema_valid=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--portable", action="store_true", help="Skip private real sample replay")
    args = parser.parse_args()
    checks = [check_case(case, args.output) for case in scenarios()]
    summary = dict(
        scope="deterministic_reconciliation_component_not_end_to_end_accuracy",
        cases=len(checks),
        passed=sum(c["passed"] for c in checks),
        idempotence_passed=sum(c["idempotent"] for c in checks),
        automatic_cases=sum(
            any(op != "PENDING_REVIEW" for op in c["expected"]["operations"]) for c in checks
        ),
        pending_cases=sum(all(op == "PENDING_REVIEW" for op in c["expected"]["operations"]) for c in checks),
        operations=dict(Counter(op for c in checks for op in c["actual"]["operations"])),
        schema_passed=len(checks),
        model_invocations=0,
        checks=checks,
    )
    if not args.portable:
        extraction = ExtractionReport.model_validate(
            load_json(ROOT / "outputs/stage4/real-final-replay.json")
        )
        normalized = NormalizationReport.model_validate(
            load_json(ROOT / "outputs/stage5/check/real-normalized.json")
        )
        assert normalize(extraction, NormalizationContext()) == normalized
        result = reconcile(normalized, extraction)
        passed = (
            len(result.decisions) == 7
            and all(d.operation == "PENDING_REVIEW" for d in result.decisions)
            and not result.next_state.bills
        )
        assert len(result.files) == len(extraction.files)
        summary["private_replay"] = dict(
            passed=passed,
            lines=len(result.decisions),
            files=len(result.files),
            posting_subject_provided=False,
            source="stage4_cached_extraction",
            new_model_or_ocr_calls=0,
        )
        write_json(args.output / "private-real-result.json", result.model_dump(mode="python"))
    summary["all_passed"] = all(c["passed"] for c in checks) and summary.get("private_replay", {}).get(
        "passed", True
    )
    write_json(args.output / "summary.json", summary)
    print(dumps({k: v for k, v in summary.items() if k != "checks"}))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
