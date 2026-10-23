"""Collect actual Stage 8 evidence; require frozen releases, independent calls and exact replay agreement."""

import hashlib
import re
import subprocess
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.stage7_acceptance import test_counts
from shipping_billing.cli import SCHEMAS
from shipping_billing.harness.dataset import inside, verify_release
from shipping_billing.io import dumps, load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/stage8"
DATA = ROOT / "tests/fixtures/harness"


def evaluation(name):
    return load_json(OUT / name / "evaluation.json")


def failed_cases(name):
    return [
        dict(round=p.parent.parent.name, **load_json(p))
        for p in sorted((OUT / name).glob("round-*/*/score.json"))
        if not load_json(p)["passed"]
    ]


def projection_check(original, replay):
    correct = total = 0
    for file in sorted((OUT / original / "round-1").glob("*/score.json")):
        before = load_json(file)
        after = load_json(OUT / replay / "round-1" / file.parent.name / "score.json")
        for left, right in zip(before["business_projection"], after["business_projection"], strict=True):
            correct += left == right
            total += 1
        if before["counts"] != after["counts"] or before["severe"] != after["severe"]:
            raise ValueError("Replay case-level counts or severe findings changed.")
    return dict(numerator=correct, denominator=total, value=correct / total)


def main():
    release = None
    for split in ("development", "validation", "holdout"):
        release = verify_release(DATA, DATA / "release-final.json", split)
    final = {
        s: evaluation(name)
        for s, name in (
            ("development", "development-final-code"),
            ("validation", "validation-final"),
            ("holdout", "holdout-final"),
        )
    }
    replay = {s: evaluation("clean-" + s) for s in final}
    for result in [*final.values(), *replay.values()]:
        if (
            not result["passed"]
            or result["code_sha256"] != release["code_sha256"]
            or result["code_changed_during_run"]
        ):
            raise ValueError("All final/replay evaluations must pass under the frozen code.")
    validation = final["validation"]
    if len(validation["rounds"]) != 3 or validation["mode"] != "live" or validation["live_model_calls"] < 42:
        raise ValueError("Validation needs three independent real model rounds.")
    if final["holdout"]["mode"] != "live" or final["holdout"]["live_model_calls"] < 15:
        raise ValueError("Final holdout must use real model calls.")
    dev, clean = test_counts(OUT / "pytest.xml"), test_counts(OUT / "pytest-clean.xml")
    if dev != clean or dev["tests"] < 428 or any(dev[k] for k in ("failures", "errors", "skipped")):
        raise ValueError("Both complete test suites must pass with matching counts.")
    agreement = {
        s: projection_check(original, "clean-" + s)
        for s, original in (
            ("development", "development-final-code"),
            ("validation", "validation-final"),
            ("holdout", "holdout-final"),
        )
    }
    if any(r["value"] != 1 for r in agreement.values()) or any(
        r["live_model_calls"] for r in replay.values()
    ):
        raise ValueError("Archived replay must preserve business outputs with zero network calls.")
    private = load_json(OUT / "real-final/summary.json")
    if not private["passed"] or private["fee_lines"] != 7:
        raise ValueError("Private real-source regression must preserve all seven fees.")
    archive_files = 0
    for name in ("development", "validation", "validation-final", "holdout"):
        root = DATA / "replay-cache" / name
        manifest = load_json(root / "cache-manifest.json")
        for entry in manifest["files"]:
            file = inside(root, entry["path"])
            if hashlib.sha256(file.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Public raw archive bytes changed.")
            archive_files += 1
    for adapter in SCHEMAS.values():
        Draft202012Validator.check_schema(adapter.json_schema(mode="validation"))
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    )
    names = {p.decode("utf-8") for p in paths.split(b"\0") if p}
    matches = sum(
        len(re.findall(rb"sk-[A-Za-z0-9]{20,}", (ROOT / p).read_bytes()))
        for p in names
        if (ROOT / p).is_file()
    )
    if matches:
        raise ValueError("Credential-like strings detected in distributable files.")
    history_names = (
        "development-v1",
        "validation-v1",
        "validation-v2",
        "validation-v3",
        "validation-v4",
        "holdout-v1",
        "consumed-holdout-regression",
    )
    history = []
    for name in history_names:
        old = evaluation(name)
        history.append(
            dict(
                name=name,
                passed=old["passed"],
                code_sha256=old["code_sha256"],
                live_model_calls=old["live_model_calls"],
                injected_model_calls=old["injected_model_calls"],
                business_consistency=old["business_consistency"],
                rounds=old["rounds"],
                failures=failed_cases(name),
            )
        )
    report = dict(
        stage=8,
        passed=True,
        scope="internal_small_synthetic_corpus_fixed_authored_ocr",
        annotation_review="assistant_authored_not_independently_business_signed",
        release=release,
        current_cases=dict(development=30, validation=15, holdout=15),
        consumed_regression_cases=15,
        evaluations=final,
        non_editable_replay=replay,
        replay_business_agreement=agreement,
        development_tests=dev,
        non_editable_tests=clean,
        private_real_regression=private,
        public_archive_files_verified=archive_files,
        structural_schemas=len(SCHEMAS),
        credential_matches=matches,
        failed_and_superseded_history=history,
        next_stage="stage9_documentation_packaging_and_delivery",
    )
    write_json(OUT / "acceptance.json", report)
    write_json(ROOT / "docs/stage8-results.json", report)
    print(
        dumps(
            {
                k: v
                for k, v in report.items()
                if k
                not in (
                    "evaluations",
                    "non_editable_replay",
                    "failed_and_superseded_history",
                    "private_real_regression",
                    "release",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
