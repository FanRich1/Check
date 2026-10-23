"""Reproducible CLI wiring check. Authored predictions are NOT pipeline output."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/stage1/smoke"
OUT.mkdir(parents=True, exist_ok=True)
case = ROOT / "tests/fixtures/synthetic/DEV-001"
authored = load_json(case / "expected.json")["expected_records"]
write_json(OUT / "authored-prediction.json", authored)
authored[0]["charge_details"]["amount"] = 41
write_json(OUT / "authored-wrong-amount.json", authored)

checks = []


def run(name, expected_exit, *args):
    p = subprocess.run(
        [sys.executable, "-m", "shipping_billing", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    checks.append(
        {
            "name": name,
            "expected_exit": expected_exit,
            "actual_exit": p.returncode,
            "passed": p.returncode == expected_exit,
        }
    )


run("help", 0, "--help")
run("offline_config", 0, "config-check", "--mode", "offline")
run("request_contract", 0, "validate", "request", case / "request.json")
run("record_contract", 0, "validate", "records", OUT / "authored-prediction.json")
run(
    "authored_equal_prediction",
    0,
    "evaluate",
    "--case",
    case,
    "--actual",
    OUT / "authored-prediction.json",
    "--report",
    OUT / "comparison-pass.json",
)
run(
    "authored_wrong_amount",
    1,
    "evaluate",
    "--case",
    case,
    "--actual",
    OUT / "authored-wrong-amount.json",
    "--report",
    OUT / "comparison-fail.json",
)
run("pipeline_not_implemented", 4, "run")
write_json(
    OUT / "smoke-report.json",
    {
        "scope": "stage1_cli_wiring_not_pipeline_accuracy",
        "pipeline_executed": False,
        "python": sys.version.split()[0],
        "dependencies": {n: version(n) for n in ["pydantic", "pydantic-settings", "langchain", "simplejson"]},
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
    },
)
print(
    f"CLI wiring checks: {sum(c['passed'] for c in checks)}/{len(checks)} passed; no billing pipeline executed."
)
raise SystemExit(not all(c["passed"] for c in checks))
