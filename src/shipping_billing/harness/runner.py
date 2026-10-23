"""Run only input specifications, then open labels for scoring. Live rounds never reuse model predictions."""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from shipping_billing.errors import BillingError
from shipping_billing.extraction.client import StructuredModelClient
from shipping_billing.extraction.engine import digest
from shipping_billing.harness.dataset import code_fingerprint, inside, load_split, verify_release
from shipping_billing.harness.scoring import aggregate, metric, score_case
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.pipeline.engine import cache_profile, run_pipeline
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext


class CountingClient:
    def __init__(self, settings, faults):
        self.real_calls = self.injected_calls = 0
        self.fault = faults.get("model")
        if self.fault == "MODEL_INVALID_STRUCTURE":
            self.client = StructuredModelClient(
                settings, runnable=RunnableLambda(lambda _: AIMessage(content="{invalid JSON"))
            )
        else:
            self.client = StructuredModelClient(settings)

    def extract(self, *args, **kwargs):
        if self.fault:
            self.injected_calls += 1
            if self.fault != "MODEL_INVALID_STRUCTURE":
                raise BillingError(self.fault, "Explicit harness fault injection.")
        else:
            self.real_calls += 1
        return self.client.extract(*args, **kwargs)


class FixedOCR:
    def __init__(self, fixture, faults):
        self.pages = fixture.get("pages", {})
        self.fault = faults.get("ocr")

    def __call__(self, content, *_):
        if self.fault:
            raise BillingError(self.fault, "Explicit harness OCR fault, no network request.")
        sha = hashlib.sha256(content).hexdigest()
        if sha not in self.pages:
            raise BillingError("OCR_FIXTURE_MISMATCH", "No fixed OCR response for the exact page bytes.")
        return self.pages[sha]


def run_case(root, case, *, settings, mode, output, storage):
    folder = inside(root, case["path"])
    data = load_json(folder / "input.json")
    result_dir = output / case["case_id"]
    result_dir.mkdir(parents=True, exist_ok=True)
    if (result_dir / "outcome.json").exists():
        raise BillingError("PREDICTION_EXISTS", "Do not overwrite independent predictions.")
    faults = data.get("faults", {})
    client = CountingClient(settings, faults)
    start = time.monotonic()
    outcome = {
        "case_id": case["case_id"],
        "mode": mode,
        "live_model_calls": 0,
        "injected_model_calls": 0,
        "error_codes": [],
        "idempotent": False,
        "execution_provenance": "live_model_fixed_authored_ocr"
        if mode == "live"
        else "archived_model_fixed_authored_ocr",
    }
    try:
        state = LedgerState(bills=data.get("existing_bills", []))
        context = ReconciliationContext.model_validate(data["context"])
        inputs = [inside(folder, p) for p in data["inputs"]]
        fixture = load_json(inside(folder, data["ocr_fixture"])) if data.get("ocr_fixture") else {"pages": {}}
        provider = FixedOCR(fixture, faults)
        result = run_pipeline(
            inputs,
            settings=settings,
            mode=mode,
            output=result_dir,
            storage=storage,
            context=context,
            state=state,
            source_id=data.get("source_id"),
            client=client,
            ocr_provider=provider,
        )
        outcome["exit_code"] = result.exit_code
        outcome["model_adapter_invocations"] = result.model_invocations
        after = LedgerState.model_validate(load_json(result_dir / "state.json"))
        # Idempotence uses exact archived drafts, not another independent live prediction.
        repeated = run_pipeline(
            inputs,
            settings=settings,
            mode="replay",
            output=result_dir / "repeat",
            storage=storage,
            context=context,
            state=after,
            source_id=data.get("source_id"),
            ocr_provider=provider,
        )
        final = LedgerState.model_validate(load_json(result_dir / "repeat/state.json"))
        outcome["idempotent"] = (
            after == final
            and repeated.counts["CREATE"] == repeated.counts["UPDATE"] == 0
            and repeated.model_invocations == 0
        )
    except ValidationError:
        outcome.update(
            exit_code=2,
            error_codes=["INVALID_OLD_STATE" if data.get("existing_bills") else "VALIDATION_FAILED"],
        )
    except BillingError as exc:
        outcome.update(exit_code=2, error_codes=[exc.code])
    except Exception:
        outcome.update(exit_code=3, error_codes=["HARNESS_EXECUTION_FAILED"])
    outcome.update(
        live_model_calls=client.real_calls,
        injected_model_calls=client.injected_calls,
        elapsed_seconds=round(time.monotonic() - start, 3),
    )
    write_json(result_dir / "outcome.json", outcome)
    # Labels first enter this execution path after every prediction has been saved.
    result = score_case(case, load_json(folder / "expected.json"), data, result_dir)
    write_json(result_dir / "score.json", result)
    return result


def consistency(rounds):
    if len(rounds) < 2:
        return metric(0, 0)
    maps = [{c["case_id"]: c["business_projection"] for c in r} for r in rounds]
    correct = total = 0
    for cid, rows in maps[0].items():
        for row in rows:
            total += 1
            match = [
                next((p for p in m.get(cid, []) if p["row_id"] == row["row_id"]), None) for m in maps[1:]
            ]
            correct += all(p == row for p in match)
    return metric(correct, total)


def evaluate_dataset(
    root, *, split, settings, output, storage, mode="live", rounds=1, workers=2, release=None
):
    root, output, storage = Path(root).resolve(), Path(output).resolve(), Path(storage).resolve()
    manifest = load_split(root, split)
    started_code_sha256 = code_fingerprint()
    if split == "holdout" and mode == "live":
        if not release:
            raise BillingError(
                "HOLDOUT_REQUIRES_RELEASE", "Freeze code and all splits before final holdout inference."
            )
        verify_release(root, Path(release), split)
    if rounds < 1 or rounds > 3 or workers < 1 or workers > 4:
        raise BillingError("INVALID_EVALUATION_ARGUMENTS", "Use 1-3 rounds and 1-4 bounded workers.")
    predictions = []
    for number in range(1, rounds + 1):
        round_output = output / f"round-{number}"
        round_storage = storage / f"round-{number}"
        if mode == "live":
            # Initialize once before workers share the immutable namespace/profile.
            cache_profile(round_storage, settings, mode)

        def task(case):
            result = run_case(
                root, case, settings=settings, mode=mode, output=round_output, storage=round_storage
            )
            print(
                f"{split} round={number} {case['case_id']} behavior={result['passed']} live_calls={result['model_calls']}",
                flush=True,
            )
            return result

        with ThreadPoolExecutor(max_workers=workers) as pool:
            cases = list(pool.map(task, manifest["cases"]))
        predictions.append(cases)
        summary = aggregate(cases)
        write_json(round_output / "summary.json", summary)
    stability = consistency(predictions)
    reports = [aggregate(c) for c in predictions]
    report = {
        "scope": "raw_document_workflow_under_explicit_fixed_ocr",
        "split": split,
        "mode": mode,
        "code_sha256": started_code_sha256,
        "code_changed_during_run": code_fingerprint() != started_code_sha256,
        "dataset_sha256": digest(manifest),
        "rounds": reports,
        "business_consistency": stability,
        "live_model_calls": sum(c["model_calls"] for r in predictions for c in r),
        "injected_model_calls": sum(c["injected_calls"] for r in predictions for c in r),
        "passed": all(r["passed"] for r in reports)
        and (rounds == 1 or stability["value"] is not None and stability["value"] >= 0.95),
        "annotation_review": manifest["annotation_review"],
        "by_format": {
            kind: aggregate([c for c in predictions[0] if kind in c["formats"]], require_coverage=False)
            for kind in sorted({k for c in manifest["cases"] for k in c["formats"]})
        },
        "by_expected_operation_case_group": {
            operation: aggregate(
                [c for c in predictions[0] if operation in c["expected_operations"]], require_coverage=False
            )
            for operation in sorted({op for c in predictions[0] for op in c["expected_operations"]})
        },
        "by_source_kind": {
            kind: aggregate([c for c in predictions[0] if c["source_kind"] == kind], require_coverage=False)
            for kind in sorted({c["source_kind"] for c in predictions[0]})
        },
    }
    if report["code_changed_during_run"]:
        report["passed"] = False
    write_json(output / "evaluation.json", report)
    print(
        dumps(
            {
                k: v
                for k, v in report.items()
                if k not in ("rounds", "by_format", "by_expected_operation_case_group", "by_source_kind")
            }
        )
    )
    return report
