"""Verify a non-editable install from an extracted delivery ZIP using public offline material."""

import argparse
import importlib.resources
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import shipping_billing
from shipping_billing.io import load_json, write_json
from shipping_billing.web.service import native_path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Use a new verification output folder")
    output.mkdir(parents=True)
    application = Path(shipping_billing.__file__).resolve()
    if "site-packages" not in application.parts or application.is_relative_to(ROOT / "src"):
        parser.error("Install with --no-editable and run with that environment's Python")
    report = dict(non_editable=True, private_samples_required=False, checks=[], passed=False)

    def run(name, command):
        print("Delivery verification: " + name, flush=True)
        with (output / (name + ".log")).open("w", encoding="utf-8") as log:
            result = subprocess.run([sys.executable, *command], cwd=ROOT, stdout=log, stderr=log)
        if result.returncode:
            raise RuntimeError(name + " failed; see its local log")
        report["checks"].append(name)

    try:
        assets = importlib.resources.files("shipping_billing.web") / "static"
        report["packaged_static_assets"] = ["index.html", "app.css", "app.js", "mark.svg"]
        if not all((assets / name).is_file() for name in report["packaged_static_assets"]):
            raise RuntimeError("Packaged web assets missing")
        run("pytest", ["-m", "pytest", "-q", "--junitxml=" + str(output / "pytest.xml")])
        suite = ET.parse(output / "pytest.xml").getroot().find("testsuite")
        report["tests"] = {k: int(suite.attrib[k]) for k in ("tests", "failures", "errors", "skipped")}
        report["unexpected_skips"] = [case.attrib["name"] for case in suite.findall("testcase")
                                      if case.find("skipped") is not None and not (
                                          case.attrib["name"] == "test_real_mysql_concurrent_commits_only_one_wins[sqlite]"
                                      )]
        run("demo", ["-m", "scripts.stage7_demo", "--mode", "replay", "--storage",
                     "tests/fixtures/pipeline/replay-cache", "--output", str(output / "demo")])
        report["demo"] = load_json(output / "demo/summary.json")
        report["benchmarks"] = {}
        for split, cache in (("development", "development"), ("validation", "validation-final"),
                             ("holdout", "holdout")):
            run(split, ["-m", "shipping_billing", "benchmark", "--split", split, "--mode", "replay",
                        "--storage", str(native_path(ROOT / "tests/fixtures/harness/replay-cache" / cache)),
                        "--output", str(output / split)])
            result = load_json(output / split / "evaluation.json")
            if not result["passed"] or result["live_model_calls"]:
                raise RuntimeError("Public offline regression failed: " + split)
            report["benchmarks"][split] = result
        report["passed"] = not any(report["tests"][k] for k in ("failures", "errors")) and not report["unexpected_skips"]
    except (OSError, RuntimeError) as exc:
        report["error"] = str(exc)
    write_json(output / "verification.json", report)
    print(dict(passed=report["passed"], checks=report["checks"], tests=report.get("tests")), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
