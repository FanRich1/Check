"""Run synthetic raw EML files through live LangChain or exact archived replay."""

import argparse
from pathlib import Path

from scripts.stage7_fixtures import eml
from shipping_billing.config import Settings
from shipping_billing.domain.contracts import NormalizationContext
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["live", "replay"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storage", type=Path, default=ROOT / "data/local/stage7/demo-cache")
    args = parser.parse_args()
    settings = Settings(_env_file=ROOT / ".env" if args.mode == "live" else None)
    source = args.output / "inputs"
    new = eml(source / "new.eml")
    amendment = eml(source / "amendment.eml", 50, note="Update amount from 40 to 50")
    context = ReconciliationContext(normalization_context=NormalizationContext(accounting_entity="Buyer Ltd"))
    results = []
    state = LedgerState()
    for name, path, expected, mode in [
        ("create", new, "CREATE", args.mode),
        ("update", amendment, "UPDATE", args.mode),
        ("repeat", amendment, "NO_CHANGE", "replay"),
    ]:
        folder = args.output / name
        result = run_pipeline(
            [path],
            settings=settings,
            output=folder,
            storage=args.storage,
            mode=mode,
            state=state,
            context=context,
            progress=lambda stage, count: print(f"{name}: {stage} items={count}", flush=True),
        )
        records = load_json(folder / "records.json")
        passed = result.exit_code == 0 and len(records) == 1 and records[0]["operation"] == expected
        passed = passed and records[0]["charge_details"]["amount"] == (40 if name == "create" else 50)
        next_state = LedgerState.model_validate(load_json(folder / "state.json"))
        if name == "repeat":
            passed = passed and next_state == state and result.model_invocations == 0
        results.append(
            dict(
                case=name,
                passed=passed,
                expected_operation=expected,
                counts=result.counts,
                model_invocations=result.model_invocations,
                mode=result.mode,
                ocr_network_calls=result.ocr_network_calls,
            )
        )
        state = next_state
        print(dumps(results[-1]), flush=True)
        if not passed:
            break
    report = dict(
        scope="synthetic_raw_eml_end_to_end",
        all_passed=len(results) == 3 and all(r["passed"] for r in results),
        cases=results,
    )
    write_json(args.output / "summary.json", report)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
