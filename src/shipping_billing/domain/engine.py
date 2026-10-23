"""Deterministic stage 5 normalization and validation; no ledger mutation or model calls."""

import re

from shipping_billing.domain import money
from shipping_billing.domain.contracts import (
    Check,
    LinePolicy,
    NormalizationContext,
    NormalizationReport,
    NormalizedFile,
    NormalizedLine,
    RuleEvent,
)
from shipping_billing.domain.evidence import EvidenceIndex, labeled_amount, labeled_currencies
from shipping_billing.domain.finance import financial_direction
from shipping_billing.domain.normalization import (
    CHARGES,
    CURRENCIES,
    SNAPSHOT,
    alias_map,
    currency,
    key,
    payment,
    resolve,
)
from shipping_billing.errors import BillingError
from shipping_billing.extraction.engine import digest
from shipping_billing.models import FinancialAttributes, Issue


def problem(code, field=None, *, blocking=True):
    return Issue(
        code=code,
        severity="warning" if blocking else "info",
        blocks_automation=blocking,
        field=field,
        message="Domain rule: " + code,
    )


def append_unique(issues, item):
    if not any((x.code, x.field) == (item.code, item.field) for x in issues):
        issues.append(item)


def validate_literals(line, index):
    c = line.candidate
    groups = ("document_info", "transaction_party", "shipping_info")
    for group in groups:
        obj = getattr(c, group)
        values = dict(obj) if group == "shipping_info" else obj.model_dump(mode="python")
        for field, value in values.items():
            if value is None:
                continue
            path = f"/{group}/{field}"
            if not index.verify(c.field_evidence.get(path, []), value, c.source_id):
                append_unique(line.issues, problem("UNSUPPORTED_FIELD_EVIDENCE", path))
                if group == "shipping_info":
                    c.shipping_info.pop(field, None)
                else:
                    setattr(obj, field, None)
    charge = c.charge_details
    for name in ("charge_item_raw", "amount", "currency_raw", "quantity", "unit_price", "payment_term_raw"):
        value = getattr(charge, name)
        if value is None:
            continue
        path = "/charge_details/" + name
        if name in ("amount", "quantity", "unit_price") and (
            len(value.as_tuple().digits) > 128 or abs(value.as_tuple().exponent) > 128
        ):
            append_unique(line.issues, problem("NUMBER_LIMIT_EXCEEDED", path))
            setattr(charge, name, None)
            continue
        if not index.verify(c.field_evidence.get(path, []), value, c.source_id):
            append_unique(line.issues, problem("UNSUPPORTED_FIELD_EVIDENCE", path))
            setattr(charge, name, None)
    for raw, normal in (
        ("charge_item_raw", "charge_item"),
        ("currency_raw", "currency"),
        ("payment_term_raw", "payment_term"),
    ):
        if getattr(charge, raw) is None and getattr(charge, normal) is not None:
            val = getattr(charge, normal)
            if index.verify(c.field_evidence.get("/charge_details/" + normal, []), val, c.source_id):
                setattr(charge, raw, val)
                c.field_evidence["/charge_details/" + raw] = list(
                    c.field_evidence["/charge_details/" + normal]
                )


def apply_corrections(line, policy, index, event):
    for correction in policy.corrections:
        path = correction.path
        original, proposed = correction.original_value, correction.corrected_value
        if path == "/document_info/reference_no":
            current = line.original.document_info.reference_no
            label = r"INVOICE|REFERENCE|D/C|单据"
        else:
            field = path.split("/")[2]
            current = line.original.shipping_info.get(field)
            if field == "container_nos":
                i = int(path.split("/")[3])
                current = current[i] if isinstance(current, list) and i < len(current) else None
            label = {
                "mbl_no": r"MBL|MASTER B/L",
                "hbl_no": r"HBL|HOUSE B/L",
                "booking_no": r"BOOKING|订舱",
                "container_nos": r"CNTR|CONTAINER|箱号",
            }[field]
        confusions = ({"0", "O"}, {"1", "I"})
        compatible = (
            len(original) == len(proposed)
            and original != proposed
            and all(a == b or {a, b} in confusions for a, b in zip(original, proposed))
        )
        text = "\n".join(e.quote for e in correction.evidence)
        supported = current == original and compatible and re.search(label, text, re.IGNORECASE)
        supported = supported and index.verify(correction.evidence, proposed, line.source_id, native=True)
        if not supported:
            append_unique(line.issues, problem("UNSUPPORTED_IDENTIFIER_CORRECTION", path))
            continue
        if path == "/document_info/reference_no":
            line.candidate.document_info.reference_no = proposed
        elif field == "container_nos":
            values = list(line.original.shipping_info[field])
            values[i] = proposed
            line.candidate.shipping_info[field] = values
        else:
            line.candidate.shipping_info[field] = proposed
        line.candidate.field_evidence[path] = correction.evidence
        event("identifier.native_corroboration", path, original, proposed, correction.evidence)


def normalize_line(candidate, source_issues, context, charges, parties, index):
    line = NormalizedLine(
        source_line_id=candidate.source_line_id,
        source_id=candidate.source_id,
        original=candidate.model_copy(deep=True),
        candidate=candidate.model_copy(deep=True),
        financial_attributes=FinancialAttributes(accounting_entity=None, posting_type=None),
        issues=list(candidate.issues) + list(source_issues),
    )
    policy = context.lines.get(candidate.source_line_id, LinePolicy())
    c = line.candidate

    def add_issue(code, field=None, blocking=True):
        append_unique(line.issues, problem(code, field, blocking=blocking))

    def event(rule, path, old, new, refs=(), pointer=None):
        if old != new:
            line.events.append(
                RuleEvent(
                    rule=rule, path=path, old=old, new=new, evidence=list(refs), context_pointer=pointer
                )
            )

    validate_literals(line, index)
    charge = c.charge_details
    raw = charge.charge_item_raw
    if raw is not None:
        if re.fullmatch(
            r"(?:\d+\s*[xX]\s*)?\d{2}\s*['′]?\s*(?:HC|HQ|GP|DC|ST|OT|FR)", raw.strip(), re.IGNORECASE
        ):
            add_issue("EQUIPMENT_CODE_NOT_CHARGE_DESCRIPTION", "/charge_details/charge_item")
        normal = resolve(raw, charges)
        event(
            "charge.exact_alias",
            "/charge_details/charge_item",
            charge.charge_item,
            normal,
            c.field_evidence.get("/charge_details/charge_item_raw", []),
            "/charge_aliases" if key(raw) in alias_map(context.charge_aliases) else None,
        )
        charge.charge_item = normal
        c.field_evidence["/charge_details/charge_item"] = c.field_evidence.get(
            "/charge_details/charge_item_raw", []
        )
    else:
        charge.charge_item = None
        add_issue("MISSING_CHARGE_ITEM", "/charge_details/charge_item")
    code, currency_issue = currency(charge.currency_raw)
    old_code = candidate.charge_details.currency
    if old_code and code and old_code != code and old_code != (charge.currency_raw or "").strip().upper():
        add_issue("CURRENCY_CONFLICT", "/charge_details/currency")
        code = None
    if currency_issue:
        add_issue(currency_issue, "/charge_details/currency")
    event(
        "currency.literal_alias",
        "/charge_details/currency",
        charge.currency,
        code,
        c.field_evidence.get("/charge_details/currency_raw", []),
    )
    charge.currency = code
    line.currency_minor_units = CURRENCIES.get(code)
    if code:
        c.field_evidence["/charge_details/currency"] = c.field_evidence.get(
            "/charge_details/currency_raw", []
        )
    for field in ("issuer", "recipient"):
        old = getattr(c.transaction_party, field)
        new = resolve(old, parties)
        event(
            "party.configured_alias",
            "/transaction_party/" + field,
            old,
            new,
            c.field_evidence.get("/transaction_party/" + field, []),
            "/party_aliases",
        )
        setattr(c.transaction_party, field, new)
    scope = policy.payment_scope
    refs = c.field_evidence.get("/charge_details/payment_term_raw", [])
    if scope == "unknown" and raw:
        if any(
            raw in e.quote and (charge.amount is None or index.verify([e], charge.amount, c.source_id))
            for e in refs
        ):
            scope = "line"
    term, condition, payment_issue = payment(
        charge.payment_term_raw,
        scope=scope,
        is_freight=key(charge.charge_item or "")
        in ("freight", "ocean freight", "sea freight", "air freight"),
    )
    if candidate.charge_details.payment_term and term != candidate.charge_details.payment_term:
        add_issue("PAYMENT_TERM_CONFLICT", "/charge_details/payment_term")
    event("payment.scoped_term", "/charge_details/payment_term", charge.payment_term, term, refs)
    charge.payment_term, line.settlement_condition = term, condition
    if term:
        c.field_evidence["/charge_details/payment_term"] = refs
    if payment_issue:
        add_issue(payment_issue, "/charge_details/payment_term", payment_issue == "PAYMENT_TERM_CONFLICT")
    apply_corrections(line, policy, index, event)
    financial_direction(line, policy, context, parties, index, add_issue, event)
    supported_adjustments = True
    adjustments = []
    for field, sign in (("tax", 1), ("discount", -1)):
        fact = getattr(policy, field)
        if fact is None:
            continue
        if not index.verify(fact.evidence, fact.value, c.source_id) or not labeled_amount(
            field, fact.value, fact.evidence
        ):
            add_issue("UNSUPPORTED_LINE_ADJUSTMENT", "/" + field)
            supported_adjustments = False
        elif labeled_currencies(field, fact.evidence) - {charge.currency}:
            add_issue("LINE_ADJUSTMENT_CURRENCY_CONFLICT", "/" + field)
            supported_adjustments = False
        else:
            adjustments.append(fact.value if sign == 1 else fact.value.copy_abs().copy_negate())
    amount = charge.amount
    if amount is None:
        add_issue("MISSING_AMOUNT", "/charge_details/amount")
    elif line.currency_minor_units is not None:
        rounding = policy.rounding or context.rounding
        quantized = money.quantized(amount, line.currency_minor_units)
        line.checks.append(
            Check(
                code="AMOUNT_MINOR_UNIT",
                status="passed" if amount == quantized else "failed",
                expected=quantized,
                actual=amount,
                message="Printed amount must fit the currency minor unit; never silently round it.",
            )
        )
        if amount != quantized:
            add_issue("AMOUNT_PRECISION_INVALID", "/charge_details/amount")
        if charge.quantity is not None and charge.unit_price is not None:
            expected = money.product(charge.quantity, charge.unit_price)
            expected = money.add([expected] + adjustments)
            if policy.adjustment and line.balance_effect in ("decrease", "none"):
                expected = expected.copy_abs().copy_negate() if expected else expected
            expected = money.expected(expected, line.currency_minor_units, rounding)
            passed = supported_adjustments and expected == amount
            line.checks.append(
                Check(
                    code="QUANTITY_PRICE",
                    status="passed" if passed else "failed",
                    expected=expected,
                    actual=amount,
                    message="Quantity times unit price plus supported tax minus supported discount.",
                )
            )
            if not passed:
                add_issue("LINE_AMOUNT_MISMATCH", "/charge_details/amount")
        else:
            line.checks.append(
                Check(
                    code="QUANTITY_PRICE",
                    status="not_checkable",
                    message="Missing optional quantity or unit price; retain the explicit line amount.",
                )
            )
    return line


def normalize(report, context=None):
    from shipping_billing.domain.totals import check_totals

    context = context or NormalizationContext()
    parties = alias_map(context.party_aliases)
    charges = alias_map({**CHARGES, **context.charge_aliases})
    index = EvidenceIndex(report)
    all_candidates = [c for f in report.files for c in f.candidates]
    identities = {c.source_line_id for c in all_candidates}
    if len(identities) != len(all_candidates):
        raise BillingError("DUPLICATE_LINE_ID", "Each extracted fee needs a unique line identity.")
    if set(context.lines) - identities:
        raise BillingError("UNKNOWN_CONTEXT_LINE", "Line policy references an unknown fee.")
    lines, files = [], []
    for source in report.files:
        if source.status == "no_charge" and source.candidates:
            raise BillingError(
                "INCONSISTENT_SOURCE_STATUS", "A no-charge source cannot contain fee candidates."
            )
        if any(c.source_id != source.source_id for c in source.candidates):
            raise BillingError("SOURCE_LINE_MISMATCH", "Candidate source must match its file.")
        source_issues = list(source.issues)
        if source.status == "pending_review" and not any(
            i.blocks_automation for i in source_issues + [i for c in source.candidates for i in c.issues]
        ):
            source_issues.append(problem("UPSTREAM_REVIEW_UNRESOLVED"))
        source_lines = [
            normalize_line(c, source_issues, context, charges, parties, index) for c in source.candidates
        ]
        if source.status == "error":
            for line in source_lines:
                append_unique(line.issues, problem("UPSTREAM_EXTRACTION_ERROR"))
        files.append(
            NormalizedFile(
                source_id=source.source_id,
                parent_source_id=source.parent_source_id,
                status="error" if source.status == "error" else "pending_review",
                line_ids=[c.source_line_id for c in source_lines],
                issues=source_issues,
            )
        )
        lines.extend(source_lines)
    result = NormalizationReport(
        currency_snapshot={k: v for k, v in SNAPSHOT.items() if k not in ("minor_units", "historical_codes")},
        input_sha256=digest(report.model_dump(mode="python")),
        context_sha256=digest(context.model_dump(mode="python")),
        files=files,
        lines=lines,
    )
    check_totals(result, report, context, index)
    for line in lines:
        line.candidate.issues = list(line.issues)
        core = line.candidate.charge_details
        good = core.charge_item is not None and core.amount is not None and core.currency is not None
        good = (
            good
            and line.financial_attributes.accounting_entity is not None
            and line.financial_attributes.posting_type is not None
        )
        line.status = (
            "ready" if good and not any(i.blocks_automation for i in line.issues) else "pending_review"
        )
    for file in files:
        if file.status == "error":
            continue
        members = [line for line in lines if line.source_id == file.source_id]
        if not members:
            file.status = "pending_review" if any(i.blocks_automation for i in file.issues) else "no_charge"
        else:
            file.status = "ready" if all(line.status == "ready" for line in members) else "pending_review"
    return result
