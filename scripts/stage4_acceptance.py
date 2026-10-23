"""Collect concrete stage 4 evidence; never mark acceptance with missing checks."""

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from jsonschema import Draft202012Validator

from shipping_billing.extraction.contracts import ExtractionReport
from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs/stage4"


def test_counts(path):
    suites = ET.parse(path).getroot()
    return {
        k: sum(int(s.attrib.get(k, 0)) for s in suites.findall("testsuite"))
        for k in ("tests", "failures", "errors", "skipped")
    }


def main():
    real = load_json(OUTPUT / "real-evaluation.json")
    validation = load_json(OUTPUT / "validation-v7-live/evaluation.json")
    replay = load_json(OUTPUT / "validation-v7-replay/evaluation.json")
    first = ExtractionReport.model_validate(load_json(OUTPUT / "real-final-replay.json"))
    second = ExtractionReport.model_validate(load_json(OUTPUT / "real-final-replay-check.json"))
    equal = first.files == second.files
    model_calls = load_json(OUTPUT / "real-live-v7.json")["model_invocations"]
    schema = load_json(ROOT / "schemas/extraction-report.schema.json")
    validator = Draft202012Validator(schema)
    reports = [first.model_dump(mode="python")]
    cases = load_json(ROOT / "tests/fixtures/extraction-validation/expected.json")["cases"]
    for case in cases:
        live = load_json(OUTPUT / "validation-v7-live" / (case["case_id"] + ".json"))
        back = load_json(OUTPUT / "validation-v7-replay" / (case["case_id"] + ".json"))
        equal = (
            equal
            and ExtractionReport.model_validate(live).files == ExtractionReport.model_validate(back).files
        )
        reports.append(live)
        model_calls += live["model_invocations"]
    schema_ok = sum(not list(validator.iter_errors(r)) for r in reports)
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    paths = paths.decode("utf-8").split("\0")
    secret_matches = checked = 0
    for name in paths:
        path = ROOT / name
        if not name or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        checked += 1
        secret_matches += len(re.findall(r"sk-[A-Za-z0-9]{20,}", text))
    tests = {
        "development": test_counts(OUTPUT / "pytest.xml"),
        "clean_environment": test_counts(OUTPUT / "pytest-clean.xml"),
    }
    passed = all(x["passed"] for x in (real, validation, replay)) and equal and schema_ok == len(reports)
    passed = (
        passed
        and not secret_matches
        and all(
            c["tests"] >= 172 and not (c["failures"] or c["errors"] or c["skipped"]) for c in tests.values()
        )
    )
    result = {
        "stage": 4,
        "date": "2026-10-23",
        "status": "component_accepted" if passed else "not_accepted",
        "stage_gate_passed": passed,
        "scope": "candidate_extraction_not_final_billing_pipeline",
        "prompt_version": first.prompt_version,
        "rules_version": first.rules_version,
        "real": real,
        "synthetic_validation": validation,
        "replay": replay,
        "tests": tests,
        "valid_report_schema": {"numerator": schema_ok, "denominator": len(reports)},
        "final_prompt_live_model_invocations": model_calls,
        "replay_business_consistent": equal,
        "replay_model_invocations": first.model_invocations + replay["model_invocations"],
        "credential_scan": {"git_candidate_text_files": checked, "matches": secret_matches},
        "independent_human_signoff": False,
        "billing_pipeline_executed": False,
        "limitations": [
            "CMA raw description includes adjacent OCA17 code",
            "OCR source issues still require review",
            "synthetic component fixtures are not end-to-end parser/OCR or final holdout acceptance",
            "email cost-allocation supplement always requires review",
            "normalization and financial decisions not implemented",
        ],
    }
    write_json(OUTPUT / "acceptance.json", result)
    write_json(
        OUTPUT / "fee-candidates.json",
        [c.model_dump(mode="python") for f in first.files for c in f.candidates],
    )
    print(
        {
            "stage_gate_passed": passed,
            "schema": f"{schema_ok}/{len(reports)}",
            "live_model_invocations": model_calls,
            "replay_consistent": equal,
            "credential_matches": secret_matches,
            "tests": tests,
        }
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
