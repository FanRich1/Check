"""One-to-one row scoring against separately authored labels; failures retain denominators."""

import re

from shipping_billing.extraction.engine import contains_value


def compact(value):
    return re.sub(r"\s+", "", value) if isinstance(value, str) else value


def score(expected, reports):
    expected_rows = sum(len(c["fees"]) for c in expected)
    predicted_rows = matched = correct = slots = evidence_ok = evidence_total = 0
    unknown_ok = unknown_total = 0
    failures, errors = [], []
    for case in expected:
        report = reports.get(case["case_id"])
        candidates = [c for f in report.files for c in f.candidates] if report else []
        predicted_rows += len(candidates)
        available = set(range(len(candidates)))
        if report:
            errors += [f.source_id for f in report.files if f.status == "error"]
        else:
            errors.append(case["case_id"])
        for fee in case["fees"]:
            unit = (
                next((u for u in report.evidence_units if u.unit_id == fee["row_unit"]), None)
                if report
                else None
            )
            hit = next(
                (
                    i
                    for i in sorted(available)
                    if unit
                    and candidates[i].source_id == fee["source_id"]
                    and any(
                        e.locator == unit.locator and e.source_id == unit.source_id
                        for e in candidates[i].field_evidence.get("/charge_details/charge_item_raw", [])
                    )
                ),
                None,
            )
            candidate = candidates[hit] if hit is not None else None
            if hit is not None:
                matched += 1
                available.remove(hit)
            else:
                failures.append(
                    {"case": case["case_id"], "row_unit": fee["row_unit"], "error": "missing_fee_row"}
                )
            actual = (
                {
                    "name": candidate.charge_details.charge_item_raw,
                    "amount": candidate.charge_details.amount,
                    "currency": candidate.charge_details.currency,
                    "reference_no": candidate.document_info.reference_no,
                    "issuer": candidate.transaction_party.issuer,
                    "recipient": candidate.transaction_party.recipient,
                }
                if candidate
                else {}
            )
            for key in ("name", "amount", "currency", "reference_no", "issuer", "recipient"):
                if fee.get(key) is None:
                    unknown_total += 1
                    unknown_ok += bool(candidate) and actual.get(key) is None
                    if candidate and actual.get(key) is not None:
                        failures.append(
                            {
                                "case": case["case_id"],
                                "field": key,
                                "error": "unexpected_known_value",
                                "actual": actual[key],
                            }
                        )
                    continue
                slots += 1
                ok = bool(candidate) and compact(fee[key]) == compact(actual.get(key))
                correct += ok
                if not ok:
                    failures.append(
                        {
                            "case": case["case_id"],
                            "row_unit": fee["row_unit"],
                            "field": key,
                            "expected": fee[key],
                            "actual": actual.get(key),
                        }
                    )
        for i in available:
            failures.append(
                {"case": case["case_id"], "error": "extra_fee_row", "line_id": candidates[i].source_line_id}
            )
        if report:
            for candidate in candidates:
                fields = {}
                for group in ("document_info", "transaction_party", "charge_details"):
                    fields.update(
                        {
                            f"/{group}/{k}": v
                            for k, v in getattr(candidate, group).model_dump(mode="python").items()
                            if v is not None
                        }
                    )
                fields.update({"/shipping_info/" + k: v for k, v in candidate.shipping_info.items()})
                for path, value in fields.items():
                    evidence_total += 1
                    refs = candidate.field_evidence.get(path, [])
                    valid = bool(refs) and all(
                        any(
                            u.source_id == e.source_id and u.locator == e.locator and e.quote in u.text
                            for u in report.evidence_units
                        )
                        for e in refs
                    )
                    valid = valid and contains_value(value, "\n".join(e.quote for e in refs))
                    evidence_ok += valid
                    if not valid:
                        failures.append(
                            {"case": case["case_id"], "field": path, "error": "unlocatable_evidence"}
                        )

    def metric(n, d):
        return {"numerator": n, "denominator": d, "value": n / d if d else None}

    precision, recall, accuracy = (
        metric(matched, predicted_rows),
        metric(matched, expected_rows),
        metric(correct, slots),
    )
    passed = (
        precision["value"] is not None
        and precision["value"] >= 0.9
        and recall["value"] is not None
        and recall["value"] >= 0.9
        and accuracy["value"] is not None
        and accuracy["value"] >= 0.9
        and evidence_ok == evidence_total
        and unknown_ok == unknown_total
        and not errors
    )
    return {
        "scope": "extraction_component_not_posting_accuracy",
        "independent_human_signoff": False,
        "cases": len(expected),
        "precision": precision,
        "recall": recall,
        "key_field_accuracy": accuracy,
        "evidence_locatable": metric(evidence_ok, evidence_total),
        "unknown_handling": metric(unknown_ok, unknown_total),
        "errors": errors,
        "failures": failures,
        "passed": passed,
    }
