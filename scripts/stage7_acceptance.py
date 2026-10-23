"""Collect workflow, live/replay, installation, schema and distribution checks."""

import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from jsonschema import Draft202012Validator

from shipping_billing.cli import SCHEMAS
from shipping_billing.io import dumps, load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/stage7"


def test_counts(path):
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {
        key: sum(int(s.get(key, 0)) for s in suites) for key in ("tests", "failures", "errors", "skipped")
    }


def main():
    checks = load_json(OUT / "check/summary.json")
    clean_checks = load_json(OUT / "clean-check/summary.json")
    live = load_json(OUT / "live-demo/summary.json")
    public = load_json(OUT / "public-demo/summary.json")
    clean_public = load_json(OUT / "clean-demo/summary.json")
    real = load_json(OUT / "real-accepted/summary.json")
    dev, clean = test_counts(OUT / "pytest.xml"), test_counts(OUT / "pytest-clean.xml")
    schema_count = 0
    for adapter in SCHEMAS.values():
        Draft202012Validator.check_schema(adapter.json_schema(mode="validation"))
        schema_count += 1
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    names = {p.decode("utf-8") for p in paths.split(b"\0") if p}
    credential_matches = sum(
        len(re.findall(rb"sk-[A-Za-z0-9]{20,}", (ROOT / p).read_bytes()))
        for p in names
        if (ROOT / p).is_file()
    )
    registration = checks["input_registration"]
    trace = checks["accepted_record_traceability"]
    passed = (
        all(r["all_passed"] for r in (checks, clean_checks, live, public, clean_public))
        and real["passed"]
        and registration["numerator"] == registration["denominator"]
        and trace["numerator"] == trace["denominator"]
        and dev == clean
        and dev["tests"] >= 349
        and dev["failures"] == dev["errors"] == dev["skipped"] == 0
        and credential_matches == 0
    )
    report = dict(
        stage=7,
        passed=passed,
        scope="workflow_acceptance_not_production_accuracy",
        workflow_cases=dict(passed=checks["passed"], total=checks["cases"], mode=checks["mode"]),
        formats=checks["formats"],
        source_registration=registration,
        accepted_record_traceability=trace,
        development_tests=dev,
        non_editable_tests=clean,
        live_synthetic_eml_cases=len(live["cases"]),
        new_live_model_calls=sum(c["model_invocations"] for c in live["cases"]),
        public_replay_cases=len(public["cases"]),
        public_replay_network_calls=sum(
            c["model_invocations"] + (c["ocr_network_calls"] or 0) for c in public["cases"]
        ),
        real_retained_fees=real["fee_lines"],
        real_registered_sources=real["sources"],
        known_private_ocr_errors=real["execution_errors"],
        structural_schemas=schema_count,
        credential_matches=credential_matches,
        next_stage="stage8_quality_evaluation",
    )
    write_json(OUT / "acceptance.json", report)
    print(dumps(report))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
