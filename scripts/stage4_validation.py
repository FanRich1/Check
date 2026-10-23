"""Run frozen synthetic component cases, then independently compare labels."""

import argparse
import hashlib
from pathlib import Path

from shipping_billing.config import Settings
from shipping_billing.extraction.client import StructuredModelClient
from shipping_billing.extraction.contracts import ExtractionReport
from shipping_billing.extraction.engine import ExtractionEngine, digest
from shipping_billing.extraction.evaluation import score
from shipping_billing.io import load_json, write_json
from shipping_billing.parsers.contracts import ParseReport

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/extraction-validation"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=["live", "replay", "score-only"], required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--storage", type=Path, default=ROOT / "data/local/stage4/validation-model")
    args = p.parse_args()
    manifest = load_json(FIXTURES / "manifest.json")
    for item in manifest["files"]:
        path = FIXTURES / item["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise SystemExit("Frozen validation material changed: " + item["path"])
    labels = load_json(FIXTURES / "expected.json")
    settings = Settings(_env_file=ROOT / ".env" if args.mode == "live" else None, model_max_tokens=4096)
    reports = {}
    for case in labels["cases"]:
        path = args.output / (case["case_id"] + ".json")
        if args.mode == "score-only":
            if path.exists():
                reports[case["case_id"]] = ExtractionReport.model_validate(load_json(path))
            continue
        # Only parsed source evidence is passed to the engine. Labels stay here in the evaluator.
        parsed = ParseReport.model_validate(load_json(FIXTURES / case["input"]))
        result = ExtractionEngine(
            StructuredModelClient(settings), mode=args.mode, storage=args.storage
        ).extract(parsed)
        reports[case["case_id"]] = result
        write_json(path, result.model_dump(mode="python"))
        print(
            case["case_id"],
            "rows",
            sum(len(f.candidates) for f in result.files),
            "calls",
            result.model_invocations,
            flush=True,
        )
    report = score(labels["cases"], reports)
    report["dataset_sha256"] = digest([labels, [load_json(FIXTURES / c["input"]) for c in labels["cases"]]])
    report["mode"] = args.mode
    report["execution_modes"] = sorted({r.mode for r in reports.values()})
    report["historical_model_invocations"] = sum(r.model_invocations for r in reports.values())
    report["model_invocations"] = (
        sum(r.model_invocations for r in reports.values()) if args.mode != "score-only" else None
    )
    write_json(args.output / "evaluation.json", report)
    print(
        {k: report[k] for k in ("precision", "recall", "key_field_accuracy", "evidence_locatable", "passed")}
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
