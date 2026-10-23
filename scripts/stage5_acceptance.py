"""Collect stage 5 rule, real-sample, schema and clean-install acceptance evidence."""

import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from jsonschema import Draft202012Validator

from shipping_billing.domain.contracts import NormalizationReport
from shipping_billing.domain.normalization import SNAPSHOT
from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/stage5"


def counts(path):
    return {
        k: sum(int(s.get(k, 0)) for s in ET.parse(path).getroot().findall("testsuite"))
        for k in ("tests", "failures", "errors", "skipped")
    }


def main():
    evaluation = load_json(OUT / "check/evaluation.json")
    paths = sorted((OUT / "check/cases").glob("*.json")) + [
        OUT / "check/real-normalized.json",
        OUT / "demo.json",
    ]
    schema = load_json(ROOT / "schemas/normalization-report.schema.json")
    validator = Draft202012Validator(schema)
    valid = 0
    for path in paths:
        obj = load_json(path)
        NormalizationReport.model_validate(obj)
        valid += not list(validator.iter_errors(obj))
    tests = {
        "development": counts(OUT / "pytest.xml"),
        "clean_wheel_environment": counts(OUT / "pytest-clean.xml"),
    }
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode("utf-8")
        .split("\0")
    )
    matches = 0
    for name in names:
        path = ROOT / name
        if name and path.is_file():
            try:
                matches += len(re.findall(r"sk-[A-Za-z0-9]{20,}", path.read_text(encoding="utf-8")))
            except UnicodeError:
                pass
    repeated = load_json(OUT / "check/real-normalized.json") == load_json(OUT / "real-repeat.json")
    clean = load_json(OUT / "check/real-normalized.json") == load_json(OUT / "clean-real.json")
    passed = evaluation["passed"] and valid == len(paths) and matches == 0 and repeated and clean
    passed = passed and all(
        t["tests"] >= 264 and t["failures"] == t["errors"] == t["skipped"] == 0 for t in tests.values()
    )
    result = {
        "stage": 5,
        "date": "2026-10-23",
        "status": "domain_rules_accepted" if passed else "not_accepted",
        "stage_gate_passed": passed,
        "scope": "normalization_money_and_AR_AP_rules_not_final_billing_decisions",
        "rules_version": "stage5-domain-v1",
        "evaluation": evaluation,
        "tests": tests,
        "valid_schema": {"numerator": valid, "denominator": len(paths)},
        "repeat_consistent": repeated,
        "clean_wheel_matches_development": clean,
        "credential_matches": matches,
        "model_invocations": 0,
        "currency_snapshot": {
            k: v for k, v in SNAPSHOT.items() if k not in ("minor_units", "historical_codes")
        },
        "independent_human_signoff": False,
        "billing_pipeline_executed": False,
        "remaining": [
            "real samples need explicit accounting entity and remaining party/OCR review",
            "historical currencies require manual/date-sensitive review",
            "no exchange-rate conversion or automatic fuzzy identifier repair",
            "matching, deduplication and authorized incremental updates are stage 6",
        ],
    }
    write_json(OUT / "acceptance.json", result)
    print(
        {
            "passed": passed,
            "rule_cases": evaluation["rules"],
            "real_fees": evaluation["real"]["fees"],
            "schema": f"{valid}/{len(paths)}",
            "tests": tests,
            "credential_matches": matches,
        }
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
