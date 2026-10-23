"""Authored synthetic rule inputs and independent expected values for stage 5.

These component facts are not model predictions or end-to-end acceptance documents.
"""

from decimal import Decimal

from shipping_billing.domain.contracts import (
    Adjustment,
    LinePolicy,
    NormalizationContext,
    SupportedFact,
)
from shipping_billing.extraction.contracts import (
    EvidenceUnit,
    ExtractedCandidate,
    ExtractionFile,
    ExtractionReport,
    Observation,
)
from shipping_billing.models import ChargeDetails, DocumentInfo, Evidence, Locator, TransactionParty


def decimal(value):
    return Decimal(str(value)) if value is not None else None


def make_input(
    *,
    name="Handling Fee",
    amount=40,
    raw_currency="USD",
    doc_type="Invoice",
    issuer="Supplier Ltd",
    recipient="Buyer Ltd",
    entity="Buyer Ltd",
    quantity=None,
    unit_price=None,
    payment_raw=None,
    rows=None,
    extra_text=None,
):
    rows = (
        rows
        if rows is not None
        else [
            dict(
                name=name,
                amount=amount,
                raw_currency=raw_currency,
                quantity=quantity,
                unit_price=unit_price,
                payment_raw=payment_raw,
            )
        ]
    )
    units, candidates = [], []
    for i, row in enumerate(rows, 1):
        text = f"Type: {doc_type}; Invoice: INV-RULE; Issuer: {issuer}; Bill to: {recipient}\n"
        values = dict(
            charge_item_raw=row.get("name", name),
            amount=decimal(row.get("amount", amount)),
            currency_raw=row.get("raw_currency", raw_currency),
            quantity=decimal(row.get("quantity")),
            unit_price=decimal(row.get("unit_price")),
            payment_term_raw=row.get("payment_raw"),
        )
        text += "; ".join(f"{k}: {v}" for k, v in values.items() if v is not None)
        unit = EvidenceUnit(
            unit_id=f"s:u{i}",
            source_id="s",
            text=text,
            locator=Locator(kind="email_body", section=f"fee-{i}"),
            method="authored_synthetic_fragment",
        )
        units.append(unit)
        ref = Evidence(source_id="s", locator=unit.locator, quote=text)
        field_evidence = {"/charge_details/" + k: [ref] for k, v in values.items() if v is not None}
        field_evidence.update({"/document_info/document_type": [ref], "/document_info/reference_no": [ref]})
        field_evidence.update(
            {"/transaction_party/" + k: [ref] for k, v in (("issuer", issuer), ("recipient", recipient)) if v}
        )
        initial_code = values["currency_raw"]
        initial_code = (
            initial_code.upper()
            if initial_code and len(initial_code) == 3 and initial_code.isascii() and initial_code.isalpha()
            else None
        )
        candidates.append(
            ExtractedCandidate(
                source_id="s",
                source_line_id=f"s:L{i}",
                document_info=DocumentInfo(
                    document_type=doc_type, reference_no="INV-RULE", document_date=None
                ),
                transaction_party=TransactionParty(issuer=issuer, recipient=recipient),
                charge_details=ChargeDetails(
                    charge_item=values["charge_item_raw"], currency=initial_code, payment_term=None, **values
                ),
                evidence=[ref],
                field_evidence=field_evidence,
            )
        )
    report = ExtractionReport(
        mode="test_injected",
        prompt_version="authored-rule-facts-v1",
        input_sha256="synthetic",
        files=[ExtractionFile(source_id="s", status="extracted", candidates=candidates)],
        evidence_units=units,
    )
    if extra_text:
        add_evidence(report, extra_text)
    return report, NormalizationContext(accounting_entity=entity)


def add_evidence(report, text, *, method="authored_synthetic_fragment"):
    unit = EvidenceUnit(
        unit_id=f"s:extra-{len(report.evidence_units)}",
        source_id="s",
        text=text,
        locator=Locator(kind="email_body", section=f"extra-{len(report.evidence_units)}"),
        method=method,
    )
    report.evidence_units.append(unit)
    return Evidence(source_id="s", locator=unit.locator, quote=text)


def add_total(report, text, kind="total"):
    ref = add_evidence(report, text)
    unit = report.evidence_units[-1]
    report.files[0].observations.append(
        Observation(kind=kind, citations=[dict(unit_id=unit.unit_id, quote=ref.quote)])
    )
    return ref


def scenarios():
    result = []

    def add(cid, kwargs, expected, setup=None):
        report, context = make_input(**kwargs)
        if setup:
            setup(report, context)
        result.append((cid, report, context, expected))

    for i, (raw, normal) in enumerate(
        [
            ("Handling Fee", "Handling Charge"),
            ("H/D Fee", "Handling Charge"),
            ("H/C", "Handling Charge"),
            ("Doc Fee", "Documentation Fee"),
            ("THC", "Terminal Handling Charge"),
            ("Trucking Fee", "Trucking Charge"),
            ("Customs Fee", "Customs Clearance Fee"),
            ("Storage Fee", "Storage Charge"),
            ("40HC", "40HC"),
            ("Unlisted equipment wash", "Unlisted equipment wash"),
            ("OCA17 On Carriage Additional - US Em", "On Carriage Additional - US Em"),
            ("4 ADD'L DETENTION FREE TIME CHARGE", "4 ADD'L DETENTION FREE TIME CHARGE"),
        ],
        1,
    ):
        add(f"name-{i:02d}", dict(name=raw), dict(name=normal, ready=raw != "40HC"))
    for i, (raw, code, ready) in enumerate(
        [
            ("usd", "USD", True),
            ("RMB", "CNY", True),
            ("US$", "USD", True),
            ("CA$", "CAD", True),
            ("CN¥", "CNY", True),
            ("JP¥", "JPY", True),
            ("€", "EUR", True),
            ("$", None, False),
            ("¥", None, False),
            ("ZZZ", None, False),
            ("XXX", "XXX", False),
            (None, None, False),
        ],
        1,
    ):
        add(f"currency-{i:02d}", dict(raw_currency=raw), dict(currency=code, ready=ready))
    for i, (raw, name, term, ready) in enumerate(
        [
            ("PP", "Handling Fee", "PP", True),
            ("CC", "Handling Fee", "CC", True),
            ("COD", "Handling Fee", None, True),
            ("FREIGHT PREPAID", "Handling Fee", None, True),
            ("FREIGHT PREPAID", "Ocean Freight", "PP", True),
            ("FREIGHT COLLECT", "Air Freight", "CC", True),
            ("NET 30", "Handling Fee", None, True),
            ("PP / CC", "Handling Fee", None, False),
        ],
        1,
    ):
        add(f"payment-{i:02d}", dict(name=name, payment_raw=raw), dict(payment=term, ready=ready))
    for i, (cur, amount, quantity, rate, rounding, ready) in enumerate(
        [
            ("USD", ".30", 3, ".10", "exact", True),
            ("USD", "10.01", 2, 5, "exact", False),
            ("JPY", 15, 3, 5, "exact", True),
            ("JPY", "15.1", None, None, "exact", False),
            ("KWD", "1.234", 1, "1.234", "exact", True),
            ("KWD", "1.2345", None, None, "exact", False),
            ("USD", "1.01", 1, "1.005", "HALF_UP", True),
            ("USD", "1.00", 1, "1.005", "HALF_EVEN", True),
            ("USD", "1.00", 1, "1.005", "exact", False),
            ("USD", "9007199254740993.12", 1, "9007199254740993.12", "exact", True),
            ("USD", 0, 0, 5, "exact", True),
        ],
        1,
    ):
        add(
            f"money-{i:02d}",
            dict(raw_currency=cur, amount=amount, quantity=quantity, unit_price=rate),
            dict(amount=decimal(amount), ready=ready),
            setup=lambda r, c, rounding=rounding: setattr(c, "rounding", rounding),
        )
    add("finance-issuer", dict(entity="Supplier Ltd"), dict(posting="AR", ready=True))
    add("finance-recipient", {}, dict(posting="AP", ready=True))
    add("finance-third", dict(entity="Unrelated Ltd"), dict(posting=None, ready=False))
    add("finance-missing", dict(entity=None), dict(posting=None, ready=False))
    add("finance-self", dict(issuer="Buyer Ltd"), dict(posting=None, ready=False))
    add("finance-unknown-party", dict(recipient=None), dict(posting=None, ready=False))
    add(
        "finance-credit-unlinked",
        dict(doc_type="Credit Note"),
        dict(posting=None, amount=decimal(40), ready=False),
    )
    add("finance-negative-unlinked", dict(amount=-40), dict(posting=None, amount=decimal(-40), ready=False))

    def credit_setup(report, context, kind="credit"):
        text = f"{kind}: original reference OLD-901. Original payer: Buyer Ltd; Original payee: Supplier Ltd"
        ref = add_evidence(report, text)
        context.lines["s:L1"] = LinePolicy(
            adjustment=Adjustment(
                kind=kind,
                original_reference="OLD-901",
                original_payer="Buyer Ltd",
                original_payee="Supplier Ltd",
                evidence=[ref],
            )
        )

    for sign in (40, -40):
        for entity, posting in (("Buyer Ltd", "AP"), ("Supplier Ltd", "AR")):
            add(
                f"finance-credit-{sign}-{posting}",
                dict(doc_type="Credit Note", amount=sign, entity=entity),
                dict(posting=posting, amount=decimal(-40), ready=True),
                credit_setup,
            )
    add(
        "finance-reversal",
        dict(doc_type="Reversal", entity="Supplier Ltd", amount=40),
        dict(posting="AR", amount=decimal(-40), ready=True),
        lambda r, c: credit_setup(r, c, "reversal"),
    )
    add(
        "finance-discount",
        dict(name="Service rebate", amount=-5),
        dict(posting="AP", amount=decimal(-5), ready=True),
        lambda r, c: credit_setup(r, c, "discount"),
    )

    def tax_setup(report, context):
        ref = add_evidence(report, "Line TAX 2; Line DISCOUNT 1")
        context.lines["s:L1"] = LinePolicy(
            tax=SupportedFact(value=2, evidence=[ref]), discount=SupportedFact(value=1, evidence=[ref])
        )

    add(
        "money-tax-discount",
        dict(amount=11, quantity=2, unit_price=5),
        dict(amount=decimal(11), ready=True),
        tax_setup,
    )
    add(
        "total-correct", {}, dict(ready=True, document_checks=True), lambda r, c: add_total(r, "TOTAL USD 40")
    )
    add(
        "total-conflict",
        {},
        dict(ready=False, blocked="TOTAL_MISMATCH"),
        lambda r, c: add_total(r, "TOTAL USD 41"),
    )
    add(
        "total-multicurrency",
        dict(
            rows=[
                dict(name="Fee A", amount=40, raw_currency="USD"),
                dict(name="Fee B", amount=8, raw_currency="EUR"),
            ]
        ),
        dict(ready=True, line_count=2, document_checks=True),
        lambda r, c: (add_total(r, "TOTAL USD 40"), add_total(r, "TOTAL EUR 8")),
    )
    add(
        "name-same-rows",
        dict(rows=[dict(name="Handling Fee", amount=40), dict(name="Handling Fee", amount=40)]),
        dict(ready=True, line_count=2),
        lambda r, c: add_total(r, "TOTAL USD 80"),
    )
    add(
        "upstream-preserved",
        {},
        dict(ready=False),
        lambda r, c: r.files[0].issues.append(dict_issue("OCR_IDENTIFIER_NOT_CORROBORATED")),
    )
    return result


def dict_issue(code):
    from shipping_billing.models import Issue

    return Issue(
        code=code, severity="warning", blocks_automation=True, message="Synthetic unresolved source issue"
    )
