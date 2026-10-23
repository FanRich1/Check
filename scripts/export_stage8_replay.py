"""Export only synthetic raw model archives, never predictions or private material."""

import argparse
import hashlib
import re
from pathlib import Path

from shipping_billing.harness.dataset import load_split
from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/fixtures/harness"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("development", "validation", "holdout"), required=True)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--name", help="Optional immutable cache snapshot name")
    args = parser.parse_args()
    report = load_json(args.evaluation / "evaluation.json")
    if report["split"] != args.split or not report["passed"] or report["code_changed_during_run"]:
        raise ValueError("Only an explicit passing synthetic evaluation can be packaged.")
    if any(c["source_kind"] != "synthetic" for c in load_split(DATA, args.split)["cases"]):
        raise ValueError("Private/real source archives must not be exported.")
    name = args.name or args.split
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Invalid cache snapshot name.")
    output = DATA / "replay-cache" / name
    if output.exists():
        raise ValueError("Do not overwrite a published replay corpus.")
    files = []
    for number in range(1, len(report["rounds"]) + 1):
        source = args.storage / f"round-{number}"
        paths = [
            source / "active-profile.json",
            *source.glob("profiles/*.json"),
            *source.glob("models/*/extraction/*.json"),
            *source.glob("models/*/intents/*.json"),
        ]
        for path in sorted(paths):
            data = path.read_bytes()
            if re.search(rb"sk-[A-Za-z0-9]{20,}", data):
                raise ValueError("Credential-like text in archive; do not export.")
            relative = Path(f"round-{number}") / path.relative_to(source)
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files.append(dict(path=relative.as_posix(), sha256=hashlib.sha256(data).hexdigest()))
    write_json(
        output / "cache-manifest.json",
        dict(
            split=args.split,
            provenance="synthetic_model_archives_not_expected_predictions",
            evaluation_code_sha256=report["code_sha256"],
            dataset_sha256=report["dataset_sha256"],
            recorded_live_model_calls=report["live_model_calls"],
            files=files,
            warning="Development replay can contain archives from earlier development attempts; evaluation inputs select exact hashes.",
        ),
    )
    print(f"Exported {len(files)} raw archive/profile files for {args.split}; no prediction arrays.")


if __name__ == "__main__":
    main()
