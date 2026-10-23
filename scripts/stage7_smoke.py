"""Authored end-to-end workflow gates. OCR/model doubles are explicitly test_injected."""

import argparse
import hashlib
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.stage7_fixtures import AuthoredModel, documents, eml, fake_ocr
from shipping_billing.config import Settings
from shipping_billing.domain.contracts import NormalizationContext
from shipping_billing.evaluation import RECORDS
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.pipeline.contracts import PipelineManifest
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    context = ReconciliationContext(normalization_context=NormalizationContext(accounting_entity="Buyer Ltd"))
    paths = documents(args.output / "inputs")
    paths["doc"] = ROOT / "tests/fixtures/pipeline/invoice.doc"
    checks = []

    def execute(cid, inputs, expected, *, code=0, state=None, ctx=None, model=None, files=None):
        folder = args.output / cid
        result = run_pipeline(
            inputs,
            settings=Settings(),
            output=folder,
            storage=args.output / "cache",
            mode="test_injected",
            context=ctx if ctx is not None else context,
            state=state,
            client=model or AuthoredModel(),
            ocr_provider=fake_ocr,
        )
        records = load_json(folder / "records.json")
        RECORDS.validate_python(records)
        Draft202012Validator(PipelineManifest.model_json_schema()).validate(
            load_json(folder / "manifest.json")
        )
        actual = {op: result.counts[op] for op in expected}
        differences = {} if actual == expected else dict(operations=dict(expected=expected, actual=actual))
        if result.exit_code != code:
            differences["exit_code"] = dict(expected=code, actual=result.exit_code)
        if files is not None and len(result.files) != files:
            differences["files"] = dict(expected=files, actual=len(result.files))
        registry = [s["source_id"] for s in load_json(folder / "parse.json")["sources"]]
        if registry != [f.source_id for f in result.files]:
            differences["source_registry"] = False
        for name, sha in result.artifact_sha256.items():
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != sha:
                differences["artifact_hash"] = name
        traceable = 0
        for record in records:
            if record["operation"] == "PENDING_REVIEW":
                continue
            refs = record["audit_trail"]["field_evidence"]
            required = [
                "/charge_details/charge_item",
                "/charge_details/amount",
                "/charge_details/currency",
                "/financial_attributes/accounting_entity",
                "/financial_attributes/posting_type",
            ]
            if all(refs.get(p) for p in required):
                traceable += 1
            else:
                differences["missing_core_evidence"] = record["record_id"]
        checks.append(
            dict(
                case_id=cid,
                passed=not differences,
                differences=differences,
                mode=result.mode,
                expected=expected,
                actual=actual,
                source_registry=dict(numerator=len(registry), denominator=len(result.files)),
                traceable_accepted_records=dict(
                    numerator=traceable, denominator=sum(r["operation"] != "PENDING_REVIEW" for r in records)
                ),
            )
        )
        return LedgerState.model_validate(load_json(folder / "state.json"))

    states = {}
    for fmt, path in paths.items():
        image = fmt in ("png", "jpg", "tiff")
        states[fmt] = execute(
            "format-" + fmt,
            [path],
            {"PENDING_REVIEW" if image else "CREATE": 1},
            code=1 if image else 0,
            files=1,
        )
    amended = eml(args.output / "inputs/amended.eml", 50, note="Update amount from 40 to 50")
    updated = execute("authorized-update", [amended], {"UPDATE": 1}, state=states["eml"])
    again = execute("repeat-update", [amended], {"NO_CHANGE": 1}, state=updated)
    if again != updated:
        checks[-1]["differences"]["repeated_state_change"] = True
        checks[-1]["passed"] = False
    unauthorized = eml(args.output / "inputs/unauthorized.eml", 50)
    execute("unauthorized-update", [unauthorized], {"PENDING_REVIEW": 1}, state=states["eml"], code=1)
    execute("missing-entity", [paths["eml"]], {"PENDING_REVIEW": 1}, ctx=ReconciliationContext(), code=1)
    duplicate = eml(args.output / "inputs/same-name.eml", rows=["Handling Fee | USD | 40 | PP"] * 2)
    execute("same-name-separate-rows", [duplicate], {"CREATE": 2}, files=1)
    bad = args.output / "inputs/broken.pdf"
    bad.write_bytes(b"not a supported document")
    execute("mixed-partial-failure", [paths["eml"], bad], {"CREATE": 1}, code=3, files=2)
    attached = eml(args.output / "inputs/with-attachment.eml", attachment=paths["xlsx"])
    execute("email-attachment-dedup", [attached], {"CREATE": 1, "NO_CHANGE": 1}, files=2)
    execute(
        "model-failure",
        [paths["eml"]],
        {"CREATE": 0},
        code=3,
        model=AuthoredModel(error="MODEL_CALL_FAILED"),
        files=1,
    )
    schema = PipelineManifest.model_json_schema()
    Draft202012Validator.check_schema(schema)
    summary = dict(
        scope="authored_workflow_tests_not_model_accuracy",
        mode="test_injected",
        cases=len(checks),
        passed=sum(c["passed"] for c in checks),
        all_passed=all(c["passed"] for c in checks),
        formats=list(paths),
        input_registration=dict(
            numerator=sum(c["source_registry"]["numerator"] for c in checks),
            denominator=sum(c["source_registry"]["denominator"] for c in checks),
        ),
        accepted_record_traceability=dict(
            numerator=sum(c["traceable_accepted_records"]["numerator"] for c in checks),
            denominator=sum(c["traceable_accepted_records"]["denominator"] for c in checks),
        ),
        checks=checks,
        network_calls=0,
    )
    write_json(args.output / "summary.json", summary)
    print(dumps({k: v for k, v in summary.items() if k != "checks"}))
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
