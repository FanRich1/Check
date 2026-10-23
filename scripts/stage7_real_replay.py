"""Private raw-file replay using prior genuine stage 3 OCR and stage 4 model archives."""

from pathlib import Path

from shipping_billing.config import Settings
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.parsers.local_text import LocalTextOCR
from shipping_billing.pipeline.engine import cache_profile, run_pipeline

ROOT = Path(__file__).resolve().parents[1]


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    storage = ROOT / "data/local/stage7/real-cache"
    settings = Settings(model_provider="deepseek", model_name="deepseek-flash")
    namespace, _ = cache_profile(storage, settings, "live")
    target = storage / "models" / namespace / "extraction"
    target.mkdir(parents=True, exist_ok=True)
    for archive in (ROOT / "data/local/stage4/model").glob("*.json"):
        (target / archive.name).write_bytes(archive.read_bytes())
    inventory = load_json(ROOT / "data/local/stage0/source-manifest.json")
    ocr = LocalTextOCR()
    results = []
    records = []
    errors = []
    for source in inventory["sources"]:
        if source.get("parent_id") is not None:
            continue
        sid = source["source_id"]
        folder = args.output / sid
        result = run_pipeline(
            [ROOT / source["path"]],
            settings=settings,
            output=folder,
            storage=storage,
            mode="replay",
            source_id=sid,
            ocr_map=ROOT / "outputs/stage3/replay-check/replay-map.json",
            local_ocr=ocr,
            progress=lambda stage, count: print(f"{sid}: {stage} items={count}", flush=True),
        )
        rows = load_json(folder / "records.json")
        records.extend(rows)
        for file in result.files:
            if file.status == "error":
                errors.extend(
                    dict(source_id=file.source_id, code=i.code)
                    for i in file.issues
                    if i.code.startswith("OCR_")
                )
        results.append(
            dict(
                source_id=sid,
                counts=result.counts,
                status=result.status,
                model_invocations=result.model_invocations,
                ocr_network_calls=result.ocr_network_calls,
                files=len(result.files),
            )
        )
        print(dumps(results[-1]), flush=True)
    expected = load_json(ROOT / "outputs/stage5/check/real-normalized.json")["lines"]
    actual = sorted(
        (r["charge_details"]["charge_item"], r["charge_details"]["amount"], r["charge_details"]["currency"])
        for r in records
    )
    expected = sorted(
        (
            r["candidate"]["charge_details"]["charge_item"],
            r["candidate"]["charge_details"]["amount"],
            r["candidate"]["charge_details"]["currency"],
        )
        for r in expected
    )
    passed = (
        actual == expected and len(records) == 7 and all(r["operation"] == "PENDING_REVIEW" for r in records)
    )
    expected_errors = [dict(source_id="src-mail/part-1.1.2", code="OCR_EMPTY_CONTENT")]
    passed = passed and errors == expected_errors and all(r["ocr_network_calls"] == 0 for r in results)
    report = dict(
        scope="private_raw_file_replay_not_new_live_calls",
        passed=passed,
        source_roots=len(results),
        sources=sum(r["files"] for r in results),
        fee_lines=len(records),
        model_invocations=sum(r["model_invocations"] for r in results),
        results=results,
        execution_errors=errors,
        expected_execution_errors=expected_errors,
    )
    write_json(args.output / "summary.json", report)
    print(dumps({k: v for k, v in report.items() if k != "results"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
