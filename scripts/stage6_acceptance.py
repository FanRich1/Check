"""Collect stage 6 gates without leaking private documents or credentials."""

import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from jsonschema import Draft202012Validator

from shipping_billing.cli import SCHEMAS
from shipping_billing.io import dumps, load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/stage6"


def tests(path):
    root = ElementTree.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    return {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}


def main():
    component = load_json(OUT / "check/summary.json")
    clean_component = load_json(OUT / "clean-check/summary.json")
    dev, clean = tests(OUT / "pytest.xml"), tests(OUT / "pytest-clean.xml")
    schemas = 0
    for adapter in SCHEMAS.values():
        Draft202012Validator.check_schema(adapter.json_schema(mode="validation"))
        schemas += 1
    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    paths = {p.decode("utf-8") for p in listed.split(b"\0") if p}
    secrets = sum(
        len(re.findall(rb"sk-[A-Za-z0-9]{20,}", (ROOT / p).read_bytes()))
        for p in paths
        if (ROOT / p).is_file()
    )
    passed = (
        component["all_passed"]
        and clean_component["all_passed"]
        and component["cases"] >= 40
        and component["cases"] == component["idempotence_passed"]
        and dev == clean
        and dev["tests"] >= 267
        and dev["failures"] == dev["errors"] == dev["skipped"] == 0
        and secrets == 0
    )
    report = dict(
        stage=6,
        passed=passed,
        scope="component_acceptance_not_end_to_end_accuracy",
        cases=component["cases"],
        passed_cases=component["passed"],
        idempotence_passed=component["idempotence_passed"],
        automatic_cases=component["automatic_cases"],
        pending_cases=component["pending_cases"],
        operations=component["operations"],
        development_tests=dev,
        non_editable_install_tests=clean,
        clean_component_passed=clean_component["all_passed"],
        structural_schemas=schemas,
        private_replay=component.get("private_replay"),
        credential_matches=secrets,
        model_or_ocr_calls=0,
        billing_pipeline_executed=False,
    )
    write_json(OUT / "acceptance.json", report)
    print(dumps(report))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
