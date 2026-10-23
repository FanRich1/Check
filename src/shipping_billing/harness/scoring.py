"""One-to-one source-locator alignment. Amounts never select a row or a matched bill."""

import hashlib
from copy import deepcopy

from pydantic import ValidationError

from shipping_billing.errors import BillingError
from shipping_billing.evaluation import RECORDS
from shipping_billing.io import dumps, load_json
from shipping_billing.models import same_json_value
from shipping_billing.reconciliation.matching import read

KEYS = (
    "/charge_details/charge_item",
    "/charge_details/amount",
    "/charge_details/currency",
    "/document_info/reference_no",
    "/transaction_party/issuer",
    "/transaction_party/recipient",
)
CORE = KEYS + (
    "/operation",
    "/financial_attributes/accounting_entity",
    "/financial_attributes/posting_type",
    "/charge_details/payment_term",
)


def metric(n, d):
    return {"numerator": n, "denominator": d, "value": n / d if d else None}


def same_locator(a, b):
    return {k: v for k, v in a.items() if v is not None} == {k: v for k, v in b.items() if v is not None}


def flatten_diff(old, new, path=""):
    if isinstance(old, dict) and isinstance(new, dict):
        result = []
        for key in sorted(old.keys() | new.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in old or key not in new:
                result.append(child)
            else:
                result.extend(flatten_diff(old[key], new[key], child))
        return result
    return [] if same_json_value(old, new) else [path]


def records_for_score(folder):
    manifest = load_json(folder / "manifest.json")
    for name, sha in manifest["artifact_sha256"].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise BillingError(
                "PREDICTION_HASH_MISMATCH", "Evaluation artifacts do not match the completion manifest."
            )
    records = load_json(folder / "records.json")
    RECORDS.validate_python(records)
    report = load_json(folder / "reconciliation.json")
    normalized = load_json(folder / "normalized.json")
    extraction = load_json(folder / "extraction.json")
    lines = {line["source_line_id"]: line for line in normalized["lines"]}
    grouped = {}
    for decision in report["decisions"]:
        grouped.setdefault(decision["record_id"], []).append(decision)
    result = []
    for record in records:
        decisions = grouped.get(record["record_id"], [])
        anchors = []
        observations = []
        for decision in decisions:
            original = lines[decision["source_line_id"]]["original"]
            anchors.extend(original["field_evidence"].get("/charge_details/charge_item_raw", []))
            observations.append(decision.get("observed", {}))
        # Matched pending outputs intentionally retain the immutable old bill; observations live separately.
        view = record
        if (
            record["operation"] == "PENDING_REVIEW"
            and record.get("existing_record_id")
            and len(observations) == 1
        ):
            view = deepcopy(observations[0])
            view["operation"] = "PENDING_REVIEW"
        result.append(dict(record=record, view=view, anchors=anchors, decisions=decisions))
    return result, report, extraction


def align(expected, actual):
    choices = {}
    for i, row in enumerate(expected):
        choices[i] = [
            j
            for j, item in enumerate(actual)
            if any(
                a["source_id"] == e["source_id"] and same_locator(a["locator"], e["locator"])
                for a in item["anchors"]
                for e in row["anchors"]
            )
        ]
    assigned = {}
    used = set()
    while True:
        singles = [
            (i, [j for j in options if j not in used]) for i, options in choices.items() if i not in assigned
        ]
        ready = [(i, o[0]) for i, o in singles if len(o) == 1]
        unique = [(i, j) for i, j in ready if sum(k == j for _, k in ready) == 1]
        if not unique:
            break
        for i, j in unique:
            assigned[i] = j
            used.add(j)
    return assigned


def score_case(case, labels, input_data, folder):
    outcome = load_json(folder / "outcome.json")
    expected = labels["rows"]
    differences = []
    severe = []
    schema = True
    actual = []
    report = {"decisions": [], "next_state": {"bills": input_data.get("existing_bills", [])}}
    extraction = {"evidence_units": []}
    if (folder / "records.json").exists():
        try:
            actual, report, extraction = records_for_score(folder)
        except (BillingError, ValidationError, KeyError, TypeError, OSError):
            schema = False
            differences.append({"kind": "invalid_output_contract"})
    elif outcome.get("exit_code") != 2:
        schema = False
    mapping = align(expected, actual)
    counts = {
        k: 0
        for k in (
            "matched",
            "key_correct",
            "key_slots",
            "unknown_correct",
            "unknown_slots",
            "operation_correct",
            "automatic_correct",
            "automatic_expected",
            "accepted_correct",
            "accepted",
            "risk_correct",
            "risk_total",
            "evidence_correct",
            "evidence_slots",
            "file_correct",
            "file_total",
            "unauthorized_fields",
            "idempotent",
            "idempotence_cases",
            "posting_correct",
            "posting_slots",
        )
    }
    counts.update(expected=len(expected), predicted=len(actual), matched=len(mapping))
    projections = []
    for i, row in enumerate(expected):
        item = actual[mapping[i]] if i in mapping else None
        view = item["view"] if item else {}
        target = row["expected"]
        for path in KEYS:
            desired = read(target, path)
            if desired is None:
                counts["unknown_slots"] += 1
                counts["unknown_correct"] += bool(item) and read(view, path) is None
                if item and read(view, path) is not None:
                    differences.append(
                        dict(
                            row=row["row_id"],
                            kind="invented_unknown_value",
                            path=path,
                            actual=read(view, path),
                        )
                    )
            else:
                counts["key_slots"] += 1
                good = bool(item) and same_json_value(read(view, path), desired)
                counts["key_correct"] += good
                if not good:
                    differences.append(
                        dict(
                            row=row["row_id"],
                            kind="key_field",
                            path=path,
                            expected=desired,
                            actual=read(view, path),
                        )
                    )
        op_ok = bool(item) and item["record"]["operation"] == target["operation"]
        if read(target, "/financial_attributes/posting_type") is not None:
            counts["posting_slots"] += 1
            counts["posting_correct"] += bool(item) and read(
                view, "/financial_attributes/posting_type"
            ) == read(target, "/financial_attributes/posting_type")
        counts["operation_correct"] += op_ok
        if not op_ok:
            differences.append(
                dict(
                    row=row["row_id"],
                    kind="operation",
                    expected=target["operation"],
                    actual=item["record"]["operation"] if item else None,
                )
            )
        full = bool(item) and all(same_json_value(read(item["record"], p), read(target, p)) for p in CORE)
        if item:
            actual_changes = [
                {k: c[k] for k in ("path", "old", "new")} for c in item["record"]["audit_trail"]["changes"]
            ]
            full = full and sorted(actual_changes, key=lambda c: c["path"]) == sorted(
                row["changes"], key=lambda c: c["path"]
            )
            if row.get("expected_record_id"):
                full = full and item["record"]["record_id"] == row["expected_record_id"]
                if (
                    item["record"]["operation"] in ("UPDATE", "NO_CHANGE")
                    and item["record"]["record_id"] != row["expected_record_id"]
                ):
                    severe.append(dict(kind="wrong_existing_target", row=row["row_id"]))
            for path, value in row.get("assert_values", {}).items():
                full = full and same_json_value(read(item["record"], path), value)
            if item["record"]["operation"] != "PENDING_REVIEW" and not full:
                differences.append(
                    dict(
                        row=row["row_id"],
                        kind="accepted_core_or_change_mismatch",
                        paths=[
                            p for p in CORE if not same_json_value(read(item["record"], p), read(target, p))
                        ],
                    )
                )
        if row["automatic"]:
            counts["automatic_expected"] += 1
            counts["automatic_correct"] += full
        if item and item["record"]["operation"] != "PENDING_REVIEW":
            counts["accepted"] += 1
            counts["accepted_correct"] += full
            if not full:
                severe.append(dict(kind="incorrect_accepted_core", row=row["row_id"]))
        projections.append(
            {
                "row_id": row["row_id"],
                "business": {p: read(item["record"], p) for p in CORE} if item else None,
                "changes": [
                    {k: c[k] for k in ("path", "old", "new")}
                    for c in item["record"]["audit_trail"]["changes"]
                ]
                if item
                else None,
                "existing_record_id": item["record"].get("existing_record_id") if item else None,
            }
        )
    extras = [j for j in range(len(actual)) if j not in mapping.values()]
    for j in extras:
        differences.append(
            dict(kind="extra_or_ambiguously_aligned_record", record_id=actual[j]["record"]["record_id"])
        )
        if actual[j]["record"]["operation"] != "PENDING_REVIEW":
            counts["accepted"] += 1
            severe.append(dict(kind="unexpected_accepted_fee", record_id=actual[j]["record"]["record_id"]))
    old = {b["record_id"]: b for b in input_data.get("existing_bills", [])}
    after = {b["record_id"]: b for b in report["next_state"]["bills"]}
    created = {item["record"]["record_id"] for item in actual if item["record"]["operation"] == "CREATE"}
    if set(after) != set(old) | created:
        severe.append({"kind": "snapshot_operation_mismatch"})

    def business(value):
        return {k: v for k, v in value.items() if k not in ("operation", "existing_record_id", "audit_trail")}

    for item in actual:
        record = item["record"]
        if record["operation"] != "PENDING_REVIEW" and not same_json_value(
            business(record), business(after.get(record["record_id"], {}))
        ):
            severe.append(dict(kind="record_snapshot_disagreement", record_id=record["record_id"]))
    desired_old = {b["record_id"]: b for b in labels["old_snapshot_after"]}
    authorized = {
        row["expected_record_id"]: {c["path"] for c in row["changes"]}
        for row in expected
        if row.get("expected_record_id")
    }
    for rid, bill in old.items():
        if rid not in after:
            severe.append(dict(kind="old_record_deleted", record_id=rid))
            continue
        changed = flatten_diff(bill, after[rid])
        unauthorized = [p for p in changed if p not in authorized.get(rid, set())]
        counts["unauthorized_fields"] += len(unauthorized)
        if unauthorized:
            severe.append(dict(kind="unauthorized_fields", record_id=rid, paths=unauthorized))
        if not same_json_value(after[rid], desired_old.get(rid)):
            differences.append(dict(kind="old_snapshot_difference", record_id=rid))
    actual_codes = set(outcome.get("error_codes", []))
    if (folder / "manifest.json").exists():
        manifest = load_json(folder / "manifest.json")
        actual_codes |= {p["code"] for f in manifest["files"] for p in f["issues"]}
        expected_status = {}
        if input_data.get("source_id"):
            expected_status[input_data["source_id"]] = "no_charge"
        for row in expected:
            for anchor in row["anchors"]:
                sid = anchor["source_id"]
                expected_status[sid] = (
                    "pending_review" if row["expected"]["operation"] == "PENDING_REVIEW" else "processed"
                )
                parent = sid
                while "/" in parent:
                    parent = parent.rsplit("/", 1)[0]
                    expected_status.setdefault(parent, "no_charge")
        if input_data.get("faults"):
            for sid in expected_status:
                expected_status[sid] = "error"
        for sid in sorted(expected_status, key=len, reverse=True):
            if "/" in sid and expected_status[sid] in ("error", "pending_review"):
                parent = sid.rsplit("/", 1)[0]
                if expected_status.get(parent) != "error":
                    expected_status[parent] = "pending_review"
        observed = {f["source_id"]: f["status"] for f in manifest["files"]}
        files_ok = observed == expected_status and len(manifest["files"]) == labels["expected_document_count"]
    else:
        files_ok = labels["request_rejection"] and outcome["exit_code"] == 2
    counts["file_total"] = 1
    counts["file_correct"] = files_ok and outcome["exit_code"] == labels["expected_exit"]
    if not counts["file_correct"]:
        differences.append(
            dict(kind="file_or_exit_status", expected=labels["expected_exit"], actual=outcome["exit_code"])
        )
    codes = set(labels.get("risk_codes", []))
    if codes:
        counts["risk_total"] = 1
        # No accepted writes, and the expected cause is explicitly present.
        blocked = not any(a["record"]["operation"] in ("CREATE", "UPDATE") for a in actual)
        good = blocked and bool(codes & actual_codes) and outcome["exit_code"] != 0
        counts["risk_correct"] = good
        if not good:
            differences.append(
                dict(kind="risk_not_intercepted", expected_any=sorted(codes), actual=sorted(actual_codes))
            )
    units = extraction["evidence_units"]
    for item in actual:
        for path in KEYS:
            if read(item["record"], path) is None:
                continue
            counts["evidence_slots"] += 1
            refs = item["record"]["audit_trail"]["field_evidence"].get(path, [])
            good = bool(refs) and all(
                (
                    e["source_id"].startswith("existing:")
                    and e["source_id"][9:] in old
                    and e["locator"]["kind"] == "existing_record"
                    and e["locator"]["pointer"] == path
                    and e["quote"] == dumps(read(old[e["source_id"][9:]], path))
                )
                or any(
                    u["source_id"] == e["source_id"]
                    and same_locator(u["locator"], e["locator"])
                    and e["quote"] in u["text"]
                    for u in units
                )
                for e in refs
            )
            counts["evidence_correct"] += good
            if not good:
                differences.append(dict(kind="unlocatable_evidence", path=path))
    if labels["idempotence"]:
        counts["idempotence_cases"] = 1
        counts["idempotent"] = bool(outcome.get("idempotent"))
        if not counts["idempotent"]:
            severe.append(dict(kind="non_idempotent"))
    # Deliberately broken model calls still count as missed fees/fields/operations above.
    # Their behavior gate checks explicit errors and absence of writes, not fabricated empty rows.
    behavior_diffs = differences
    if labels.get("fault_expected"):
        behavior_diffs = [d for d in differences if d["kind"] not in ("key_field", "operation")]
    return dict(
        case_id=case["case_id"],
        split=case["split"],
        family_id=case["family_id"],
        source_kind=case.get("source_kind", "synthetic"),
        formats=case["formats"],
        expected_operations=sorted({row["expected"]["operation"] for row in expected}),
        passed=not behavior_diffs and not severe and schema,
        contract_valid=schema,
        counts=counts,
        differences=differences,
        severe=severe,
        business_projection=projections,
        model_calls=outcome["live_model_calls"],
        injected_calls=outcome.get("injected_model_calls", 0),
    )


def aggregate(cases, *, require_coverage=True):
    counts = {}
    for case in cases:
        for key, value in case["counts"].items():
            counts[key] = counts.get(key, 0) + value
    pairs = {
        "precision": ("matched", "predicted"),
        "recall": ("matched", "expected"),
        "key_field_accuracy": ("key_correct", "key_slots"),
        "unknown_fields": ("unknown_correct", "unknown_slots"),
        "operation_accuracy": ("operation_correct", "expected"),
        "automatic_completion": ("automatic_correct", "automatic_expected"),
        "accepted_core_accuracy": ("accepted_correct", "accepted"),
        "risk_interception": ("risk_correct", "risk_total"),
        "evidence_locatable": ("evidence_correct", "evidence_slots"),
        "file_status": ("file_correct", "file_total"),
        "idempotence": ("idempotent", "idempotence_cases"),
        "posting_accuracy": ("posting_correct", "posting_slots"),
    }
    metrics = {name: metric(counts.get(a, 0), counts.get(b, 0)) for name, (a, b) in pairs.items()}
    targets = {
        "precision": 0.95,
        "recall": 0.95,
        "key_field_accuracy": 0.95,
        "operation_accuracy": 0.95,
        "automatic_completion": 0.90,
        "accepted_core_accuracy": 1,
        "risk_interception": 1,
        "evidence_locatable": 1,
        "file_status": 1,
        "idempotence": 1,
    }
    failures = [
        name
        for name, limit in targets.items()
        if (metrics[name]["value"] is None and require_coverage)
        or (metrics[name]["value"] is not None and metrics[name]["value"] < limit)
    ]
    severe = [dict(case_id=c["case_id"], **s) for c in cases for s in c["severe"]]
    return dict(
        cases=len(cases),
        behavior_passed=sum(c["passed"] for c in cases),
        counts=counts,
        metrics=metrics,
        severe=severe,
        unmet_thresholds=failures,
        passed=not failures and not severe and all(c["passed"] for c in cases),
    )
