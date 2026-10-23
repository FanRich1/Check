"""Currency-isolated document checks; totals and paid amounts never become fee rows."""

import re
from decimal import Decimal

from shipping_billing.domain import money
from shipping_billing.domain.contracts import Check, DocumentTotals, SupportedFact
from shipping_billing.domain.engine import append_unique, problem
from shipping_billing.domain.evidence import labeled_amount, labeled_currencies
from shipping_billing.domain.normalization import CURRENCIES, currency
from shipping_billing.errors import BillingError
from shipping_billing.models import Evidence

NUMBER = re.compile(r"(?<![\w.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![\w.])")


def observed_groups(result, extraction, context, index):
    groups = []
    explicitly_scoped = {g.source_id for g in context.totals}
    for source in extraction.files:
        members = [line for line in result.lines if line.source_id == source.source_id]
        if not members or source.source_id in explicitly_scoped:
            continue
        observations = [o for o in source.observations if o.kind in ("total", "paid_amount")]
        if not observations:
            result.document_checks.append(
                Check(
                    code="TOTAL_NOT_PROVIDED",
                    status="not_checkable",
                    message="No document total supplied; explicit line amounts retained.",
                )
            )
            continue
        references = {line.candidate.document_info.reference_no for line in members}
        if len(references) != 1:
            for line in members:
                append_unique(line.issues, problem("TOTAL_SCOPE_AMBIGUOUS"))
            continue
        facts = {}
        failures = []
        for obs in observations:
            for citation in obs.citations:
                unit = next((u for u in index.units if u.unit_id == citation.unit_id), None)
                if unit is None:
                    failures.append("UNSUPPORTED_TOTAL_EVIDENCE")
                    continue
                evidence = Evidence(source_id=unit.source_id, locator=unit.locator, quote=citation.quote)
                if not index.verify([evidence], None, source.source_id):
                    failures.append("UNSUPPORTED_TOTAL_EVIDENCE")
                    continue
                text = citation.quote
                numbers = NUMBER.findall(text)
                codes = {token for token in re.findall(r"\b[A-Z]{3}\b", text.upper()) if token in CURRENCIES}
                if len(numbers) != 1 or len(codes) > 1:
                    failures.append("TOTAL_OBSERVATION_UNPARSED")
                    continue
                normalized_codes = {line.candidate.charge_details.currency for line in members}
                if codes:
                    code = next(iter(codes))
                    raw, currency_refs = code, [evidence]
                elif len(normalized_codes) == 1 and None not in normalized_codes:
                    code = next(iter(normalized_codes))
                    raw = members[0].candidate.charge_details.currency_raw
                    currency_refs = members[0].candidate.field_evidence.get(
                        "/charge_details/currency_raw", []
                    )
                else:
                    failures.append("TOTAL_CURRENCY_AMBIGUOUS")
                    continue
                token = text.upper()
                kind = (
                    "paid"
                    if obs.kind == "paid_amount" or re.search(r"\bPAID AMOUNT\b", token)
                    else "balance"
                    if "BALANCE" in token
                    else "subtotal"
                    if re.search(r"SUB[ -]?TOTAL", token)
                    else "total"
                )
                value = Decimal(numbers[0].replace(",", ""))
                existing = facts.setdefault(code, {"currency": raw, "currency_evidence": currency_refs})
                if kind in existing and existing[kind].value != value:
                    failures.append("TOTAL_FACT_CONFLICT")
                    continue
                existing[kind] = SupportedFact(value=value, evidence=[evidence])
        for error in sorted(set(failures)):
            blocking = error != "TOTAL_OBSERVATION_UNPARSED" or not facts
            for line in members:
                append_unique(line.issues, problem(error, blocking=blocking))
        if any(line.candidate.charge_details.currency is None for line in members):
            for line in members:
                append_unique(line.issues, problem("TOTAL_UNRESOLVED_LINE_CURRENCY"))
        for code, fields in facts.items():
            scoped = [
                line.source_line_id for line in members if line.candidate.charge_details.currency == code
            ]
            if not scoped:
                for line in members:
                    append_unique(line.issues, problem("TOTAL_CURRENCY_CONFLICT"))
                continue
            groups.append(
                DocumentTotals(
                    group_id=f"observed:{source.source_id}:{code}",
                    source_id=source.source_id,
                    line_ids=scoped,
                    **fields,
                )
            )
    return groups


def check_totals(result, extraction, context, index):
    groups = list(context.totals)
    if context.use_observed_totals:
        groups += observed_groups(result, extraction, context, index)
    if len({g.group_id for g in groups}) != len(groups):
        raise BillingError("DUPLICATE_TOTAL_GROUP", "Total group identities must be unique.")
    by_id = {line.source_line_id: line for line in result.lines}
    for group in groups:
        if any(k not in by_id or by_id[k].source_id != group.source_id for k in group.line_ids):
            raise BillingError(
                "INVALID_TOTAL_SCOPE", "Totals must reference actual fee lines in their source."
            )
        members = [by_id[k] for k in group.line_ids]

        def fail(code):
            for line in members:
                append_unique(line.issues, problem(code, "/document_totals/" + group.group_id))
            result.document_checks.append(
                Check(code=code, status="failed", message="Document scope: " + group.group_id)
            )

        code, error = currency(group.currency)
        if error or not index.verify(group.currency_evidence, group.currency, group.source_id):
            fail("INVALID_TOTAL_CURRENCY_EVIDENCE")
            continue
        if any(line.candidate.charge_details.currency != code for line in members):
            fail("MIXED_CURRENCY_TOTAL")
            continue
        facts = {k: getattr(group, k) for k in ("subtotal", "total", "tax", "discount", "paid", "balance")}
        if any(
            f is not None and not index.verify(f.evidence, f.value, group.source_id) for f in facts.values()
        ):
            fail("UNSUPPORTED_TOTAL_EVIDENCE")
            continue
        if any(
            f is not None and not labeled_amount(k, f.value, f.evidence, naked_total=k == "total")
            for k, f in facts.items()
        ):
            fail("TOTAL_FACT_ROLE_UNSUPPORTED")
            continue
        if any(f is not None and labeled_currencies(k, f.evidence) - {code} for k, f in facts.items()):
            fail("MIXED_CURRENCY_ADJUSTMENT")
            continue
        if any(line.candidate.charge_details.amount is None for line in members):
            fail("TOTAL_HAS_MISSING_AMOUNT")
            continue
        minor = CURRENCIES[code]
        invalid_precision = [f for f in facts.values() if f and f.value != money.quantized(f.value, minor)]
        if invalid_precision:
            fail("TOTAL_PRECISION_INVALID")
            continue
        credit = all(line.balance_effect == "decrease" for line in members)

        def stated(name):
            fact = facts[name]
            if fact is None:
                return None
            value = fact.value
            return value.copy_abs().copy_negate() if credit and value else value

        summed = money.add(line.candidate.charge_details.amount for line in members)
        checks = []
        if facts["subtotal"]:
            checks.append(("SUBTOTAL", summed, stated("subtotal"), facts["subtotal"].evidence))
        total = money.add(
            [summed]
            + ([stated("tax")] if facts["tax"] else [])
            + (
                [stated("discount").copy_abs() if credit else stated("discount").copy_abs().copy_negate()]
                if facts["discount"]
                else []
            )
        )
        if facts["total"]:
            checks.append(("TOTAL", total, stated("total"), facts["total"].evidence))
        if facts["paid"] and facts["balance"]:
            balance = money.add([stated("total") if facts["total"] else total, stated("paid").copy_negate()])
            checks.append(
                ("BALANCE", balance, stated("balance"), facts["paid"].evidence + facts["balance"].evidence)
            )
        if not checks:
            result.document_checks.append(
                Check(
                    code="TOTAL_INCOMPLETE",
                    status="not_checkable",
                    message="Paid/balance facts are retained but insufficient for arithmetic: "
                    + group.group_id,
                )
            )
        for name, expected, actual, evidence in checks:
            expected = money.expected(expected, minor, context.rounding)
            ok = expected == actual
            result.document_checks.append(
                Check(
                    code="DOCUMENT_" + name,
                    status="passed" if ok else "failed",
                    expected=expected,
                    actual=actual,
                    evidence=evidence,
                    message="Currency-isolated document scope: " + group.group_id,
                )
            )
            if not ok:
                for line in members:
                    append_unique(
                        line.issues, problem(name + "_MISMATCH", "/document_totals/" + group.group_id)
                    )
