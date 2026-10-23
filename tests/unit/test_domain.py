from copy import deepcopy
from decimal import Decimal

import pytest

from scripts.stage5_cases import add_evidence, add_total, make_input, scenarios
from shipping_billing.domain.contracts import DocumentTotals, IdentifierCorrection, LinePolicy, SupportedFact
from shipping_billing.domain.engine import normalize
from shipping_billing.errors import BillingError

CASES = scenarios()


@pytest.mark.parametrize(
    "title,posting,ready",
    [
        ("Invoice / ACCOUNT STATEMENT", "AP", True),
        ("ACCOUNT STATEMENT | Invoice", "AP", True),
        ("Invoice / Credit Note", None, False),
        ("Invoice / Unknown Memo", None, False),
        ("ACCOUNT STATEMENT", None, False),
    ],
)
def test_compound_document_type_keeps_raw_title_and_requires_unambiguous_role(title, posting, ready):
    source, context = make_input(doc_type=title)
    line = normalize(source, context).lines[0]
    assert line.original.document_info.document_type == title
    assert line.candidate.document_info.document_type == ("Invoice" if ready else title)
    if ready:
        assert any(e.rule == "document.explicit_title" and e.old == title for e in line.events)
    assert line.financial_attributes.posting_type == posting
    assert (line.status == "ready") is ready


@pytest.mark.parametrize(
    "raw,normalized,blocked",
    [
        ("2026-10-20", "2026-10-20", False),
        ("2026-02-30", None, True),
        ("03/04/2026", None, False),
    ],
)
def test_explicit_iso_date_only(raw, normalized, blocked):
    source, context = make_input()
    candidate = source.files[0].candidates[0]
    candidate.document_info.document_date_raw = raw
    candidate.field_evidence["/document_info/document_date_raw"] = [
        add_evidence(source, "Invoice date: " + raw)
    ]
    line = normalize(source, context).lines[0]
    assert line.candidate.document_info.document_date == normalized
    assert any(i.code == "INVALID_DOCUMENT_DATE" for i in line.issues) is blocked


def test_short_payment_quote_requires_own_physical_row():
    source, context = make_input(payment_raw="PP")
    candidate = source.files[0].candidates[0]
    ref = candidate.field_evidence["/charge_details/payment_term_raw"][0].model_copy(deep=True)
    ref.quote = "PP"
    candidate.field_evidence["/charge_details/payment_term_raw"] = [ref]
    assert normalize(source, context).lines[0].candidate.charge_details.payment_term == "PP"
    # A real PP header is not a payment instruction for every fee below it.
    candidate.field_evidence["/charge_details/payment_term_raw"] = [add_evidence(source, "PP")]
    assert normalize(source, context).lines[0].candidate.charge_details.payment_term is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Item ROW-12: Handling Fee", "Handling Charge"),
        ("Item 9: Repeated service", "Repeated service"),
        ("Item Cargo: Handling Fee", "Item Cargo: Handling Fee"),
        ("Cargo ROW-12: Handling Fee", "Cargo ROW-12: Handling Fee"),
    ],
)
def test_explicit_numbered_row_label_is_not_fee_name(raw, expected):
    source, context = make_input(name=raw)
    line = normalize(source, context).lines[0]
    assert line.candidate.charge_details.charge_item_raw == raw
    assert line.candidate.charge_details.charge_item == expected


@pytest.mark.parametrize("raw", ["CODE-18 / Handling Fee", "CODE-18 / Handling Fee / line total"])
def test_slash_row_layout_needs_verified_description_and_matching_amount(raw):
    source, context = make_input(name=raw, amount=40, raw_currency="USD")
    candidate = source.files[0].candidates[0]
    ref = add_evidence(source, "CODE-18 / Handling Fee / line total USD 40; payment PP")
    candidate.field_evidence["/charge_details/charge_item_raw"] = [ref]
    line = normalize(source, context).lines[0]
    assert line.candidate.charge_details.charge_item_raw == raw
    assert line.candidate.charge_details.charge_item == "Handling Charge"
    assert any(e.rule == "charge.explicit_row_label" and e.old == raw for e in line.events)
    source.evidence_units[-1].text = "CODE-18 / Handling Fee / line total USD 99; payment PP"
    candidate.field_evidence["/charge_details/charge_item_raw"][0].quote = source.evidence_units[-1].text
    assert normalize(source, context).lines[0].candidate.charge_details.charge_item == raw


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_authored_stage5_rule_case(case):
    _, source, context, expected = deepcopy(case)
    before = source.model_dump(mode="python")
    result = normalize(source, context)
    assert source.model_dump(mode="python") == before
    assert len(result.lines) == expected.get("line_count", 1)
    line = result.lines[0]
    charge = line.candidate.charge_details
    for field, actual in (
        ("name", charge.charge_item),
        ("amount", charge.amount),
        ("currency", charge.currency),
        ("payment", charge.payment_term),
        ("posting", line.financial_attributes.posting_type),
    ):
        if field in expected:
            assert actual == expected[field], (field, actual, expected[field])
    assert (line.status == "ready") == expected["ready"], [i.code for i in line.issues]
    if "blocked" in expected:
        assert any(i.code == expected["blocked"] and i.blocks_automation for i in line.issues)
    if expected.get("document_checks"):
        assert any(c.status == "passed" and c.code.startswith("DOCUMENT_") for c in result.document_checks)
    assert line.original == source.files[0].candidates[0]


def test_context_aliases_are_explicit_and_cycles_rejected():
    source, context = make_input(issuer="SHA", recipient="TAR", entity="Legal Buyer")
    context.party_aliases = {"SHA": "Legal Supplier", "TAR": "Legal Buyer"}
    line = normalize(source, context).lines[0]
    assert line.status == "ready" and line.financial_attributes.posting_type == "AP"
    assert line.original.transaction_party.issuer == "SHA"
    assert line.candidate.transaction_party.issuer == "Legal Supplier"
    context.party_aliases = {"SHA": "TAR", "TAR": "SHA"}
    with pytest.raises(BillingError, match="cycle"):
        normalize(source, context)


def test_precision_never_uses_binary_float_or_shared_decimal_context():
    from decimal import localcontext

    source, context = make_input(
        amount=Decimal("123456789012345678901234567890.12"),
        quantity=1,
        unit_price=Decimal("123456789012345678901234567890.12"),
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 4
        line = normalize(source, context).lines[0]
    assert line.status == "ready" and line.candidate.charge_details.amount == Decimal(
        "123456789012345678901234567890.12"
    )


def test_explicit_multicurrency_total_is_blocked():
    source, context = make_input(
        rows=[dict(name="A", amount=40, raw_currency="USD"), dict(name="B", amount=8, raw_currency="EUR")]
    )
    ref = add_total(source, "TOTAL USD 48")
    context.totals = [
        DocumentTotals(
            group_id="declared",
            source_id="s",
            line_ids=["s:L1", "s:L2"],
            currency="USD",
            currency_evidence=[ref],
            total=SupportedFact(value=48, evidence=[ref]),
        )
    ]
    result = normalize(source, context)
    assert all(line.status == "pending_review" for line in result.lines)
    assert any(c.code == "MIXED_CURRENCY_TOTAL" for c in result.document_checks)


def test_invoice_tax_discount_paid_and_balance():
    source, context = make_input(amount=40)
    ref = add_evidence(source, "USD subtotal 40; Tax 4; Discount 2; Total 42; Paid 10; Balance 32")
    context.totals = [
        DocumentTotals(
            group_id="totals",
            source_id="s",
            line_ids=["s:L1"],
            currency="USD",
            currency_evidence=[ref],
            **{
                key: SupportedFact(value=value, evidence=[ref])
                for key, value in dict(subtotal=40, tax=4, discount=2, total=42, paid=10, balance=32).items()
            },
        )
    ]
    result = normalize(source, context)
    assert result.lines[0].status == "ready"
    assert {check.code for check in result.document_checks} == {
        "DOCUMENT_SUBTOTAL",
        "DOCUMENT_TOTAL",
        "DOCUMENT_BALANCE",
    }
    assert all(c.status == "passed" for c in result.document_checks)
    assert len(result.lines) == 1


def test_invalid_field_cannot_become_ready_by_normalization():
    source, context = make_input()
    source.files[0].candidates[0].charge_details.amount = Decimal(4000)
    line = normalize(source, context).lines[0]
    assert line.candidate.charge_details.amount is None
    assert line.status == "pending_review"
    assert any(i.code == "UNSUPPORTED_FIELD_EVIDENCE" for i in line.issues)


def test_correction_requires_native_literal_and_only_ocr_confusion():
    source, context = make_input()
    candidate = source.files[0].candidates[0]
    raw_ref = add_evidence(source, "MBL: ABO123456", method="local_ocr_spatial_row")
    candidate.shipping_info["mbl_no"] = "ABO123456"
    candidate.field_evidence["/shipping_info/mbl_no"] = [raw_ref]
    corrected = add_evidence(source, "MBL: AB0123456", method="pdf_native_text")
    context.lines["s:L1"] = LinePolicy(
        corrections=[
            IdentifierCorrection(
                path="/shipping_info/mbl_no",
                original_value="ABO123456",
                corrected_value="AB0123456",
                evidence=[corrected],
            )
        ]
    )
    line = normalize(source, context).lines[0]
    assert line.candidate.shipping_info["mbl_no"] == "AB0123456" and line.status == "ready"
    assert line.original.shipping_info["mbl_no"] == "ABO123456"
    source.evidence_units[-1].method = "local_ocr_spatial_row"
    line = normalize(source, context).lines[0]
    assert line.candidate.shipping_info["mbl_no"] == "ABO123456" and line.status == "pending_review"


def test_unknown_context_line_is_request_error():
    source, context = make_input()
    context.lines["missing"] = LinePolicy()
    with pytest.raises(BillingError, match="unknown fee"):
        normalize(source, context)


@pytest.mark.parametrize(
    "text,value", [("TAX 20%", 20), ("TAX 2; DISCOUNT 1", 1), ("VAT 19.5%", Decimal("19.5"))]
)
def test_tax_role_or_rate_is_not_an_amount(text, value):
    source, context = make_input(amount=60, quantity=2, unit_price=20)
    ref = add_evidence(source, text)
    context.lines["s:L1"] = LinePolicy(tax=SupportedFact(value=value, evidence=[ref]))
    result = normalize(source, context)
    assert result.lines[0].status == "pending_review"
    assert any(i.code == "UNSUPPORTED_LINE_ADJUSTMENT" for i in result.lines[0].issues)


def test_unsupported_tax_still_blocks_without_quantity_and_price():
    source, context = make_input()
    ref = add_evidence(source, "TAX 20%")
    context.lines["s:L1"] = LinePolicy(tax=SupportedFact(value=20, evidence=[ref]))
    assert normalize(source, context).lines[0].status == "pending_review"


def test_foreign_currency_line_tax_is_not_added_to_usd():
    source, context = make_input(amount=42, quantity=2, unit_price=20)
    ref = add_evidence(source, "TAX EUR 2")
    context.lines["s:L1"] = LinePolicy(tax=SupportedFact(value=2, evidence=[ref]))
    line = normalize(source, context).lines[0]
    assert any(i.code == "LINE_ADJUSTMENT_CURRENCY_CONFLICT" for i in line.issues)
    assert line.status == "pending_review"


def test_document_tax_currency_conflict_and_paid_amount_role():
    source, context = make_input()
    ref = add_evidence(source, "Total USD 42; TAX EUR 2; Paid USD 40")
    context.totals = [
        DocumentTotals(
            group_id="x",
            source_id="s",
            line_ids=["s:L1"],
            currency="USD",
            currency_evidence=[ref],
            total=SupportedFact(value=42, evidence=[ref]),
            tax=SupportedFact(value=2, evidence=[ref]),
        )
    ]
    assert any(c.code == "MIXED_CURRENCY_ADJUSTMENT" for c in normalize(source, context).document_checks)
    context.totals[0].tax = None
    context.totals[0].total.value = Decimal(40)
    assert any(c.code == "TOTAL_FACT_ROLE_UNSUPPORTED" for c in normalize(source, context).document_checks)


def test_two_different_totals_are_not_resolved_by_first_match():
    source, context = make_input()
    add_total(source, "TOTAL USD 40")
    add_total(source, "TOTAL USD 41")
    line = normalize(source, context).lines[0]
    assert line.status == "pending_review"
    assert any(i.code == "TOTAL_FACT_CONFLICT" for i in line.issues)


def test_missing_currency_row_is_not_removed_from_total_scope():
    source, context = make_input(
        rows=[dict(name="A", amount=40, raw_currency="USD"), dict(name="B", amount=8, raw_currency="$")]
    )
    add_total(source, "TOTAL USD 40")
    lines = normalize(source, context).lines
    assert all(line.status == "pending_review" for line in lines)
    assert all(any(i.code == "TOTAL_UNRESOLVED_LINE_CURRENCY" for i in line.issues) for line in lines)


def test_beneficiary_or_shipper_is_not_an_invoice_issuer():
    source, context = make_input()
    candidate = source.files[0].candidates[0]
    ref = add_evidence(source, "SHIPPER: Supplier Ltd; CONSIGNEE: Buyer Ltd")
    candidate.field_evidence["/transaction_party/issuer"] = [ref]
    candidate.field_evidence["/transaction_party/recipient"] = [ref]
    line = normalize(source, context).lines[0]
    assert line.status == "pending_review" and line.financial_attributes.posting_type is None


def test_explicit_roles_can_support_direction_for_a_notice():
    from shipping_billing.domain.contracts import SettlementRoles

    source, context = make_input(doc_type="Notice", issuer=None, recipient=None, entity="Buyer Ltd")
    ref = add_evidence(source, "Payer: Buyer Ltd; Payee: Supplier Ltd")
    context.lines["s:L1"] = LinePolicy(
        settlement=SettlementRoles(payer="Buyer Ltd", payee="Supplier Ltd", evidence=[ref])
    )
    assert normalize(source, context).lines[0].status == "ready"
    context.lines["s:L1"].settlement.payer = "Supplier Ltd"
    context.lines["s:L1"].settlement.payee = "Buyer Ltd"
    line = normalize(source, context).lines[0]
    assert line.financial_attributes.posting_type is None and line.status == "pending_review"


def test_original_relation_conflict_does_not_sign_a_credit():
    case = next(c for c in scenarios() if c[0] == "finance-credit-40-AP")
    _, source, context, _ = deepcopy(case)
    context.lines["s:L1"].adjustment.original_reference = "INV-RULE"
    line = normalize(source, context).lines[0]
    assert line.status == "pending_review" and line.candidate.charge_details.amount == 40


def test_credit_document_total_uses_negative_effect_once():
    _, source, context, _ = deepcopy(next(c for c in scenarios() if c[0] == "finance-credit-40-AR"))
    add_total(source, "TOTAL USD 40")
    result = normalize(source, context)
    assert result.lines[0].candidate.charge_details.amount == -40
    assert result.lines[0].financial_attributes.posting_type == "AR"
    assert result.lines[0].status == "ready"
    assert next(c for c in result.document_checks if c.code == "DOCUMENT_TOTAL").expected == -40


def test_unknown_payment_scope_retains_raw_and_null():
    source, context = make_input(payment_raw="PP")
    candidate = source.files[0].candidates[0]
    candidate.field_evidence["/charge_details/payment_term_raw"] = [add_evidence(source, "Payment: PP")]
    line = normalize(source, context).lines[0]
    assert line.candidate.charge_details.payment_term is None
    assert line.candidate.charge_details.payment_term_raw == "PP"
    assert any(i.code == "PAYMENT_SCOPE_UNCLEAR" for i in line.issues)


def test_canonical_currency_conflict_is_retained_for_review():
    source, context = make_input()
    source.files[0].candidates[0].charge_details.currency = "EUR"
    line = normalize(source, context).lines[0]
    assert line.candidate.charge_details.currency is None
    assert line.original.charge_details.currency == "EUR"
    assert any(i.code == "CURRENCY_CONFLICT" for i in line.issues)


def test_duplicate_source_line_is_not_silently_booked_twice():
    source, context = make_input()
    source.files[0].candidates.append(deepcopy(source.files[0].candidates[0]))
    with pytest.raises(BillingError):
        normalize(source, context)


def test_pending_upstream_status_cannot_become_ready_without_reason():
    source, context = make_input()
    source.files[0].status = "pending_review"
    result = normalize(source, context)
    assert result.lines[0].status == "pending_review"
    assert any(i.code == "UPSTREAM_REVIEW_UNRESOLVED" for i in result.lines[0].issues)


def test_source_error_and_no_charge_remain_separate():
    source, context = make_input()
    source.files[0].status = "error"
    assert normalize(source, context).files[0].status == "error"
    source.files[0].candidates = []
    source.files[0].status = "no_charge"
    result = normalize(source, context)
    assert result.files[0].status == "no_charge" and not result.lines


def test_giant_numbers_are_bounded_before_arithmetic():
    source, context = make_input(amount=Decimal("1E+200"))
    line = normalize(source, context).lines[0]
    assert line.status == "pending_review"
    assert any(i.code == "NUMBER_LIMIT_EXCEEDED" for i in line.issues)


def test_historical_currency_is_reviewed_without_guessing_precision():
    source, context = make_input(raw_currency="DEM")
    line = normalize(source, context).lines[0]
    assert line.candidate.charge_details.currency is None
    assert any(i.code == "HISTORICAL_CURRENCY_REQUIRES_REVIEW" for i in line.issues)


@pytest.mark.parametrize(
    "raw,canonical",
    [("charge_item_raw", "charge_item"), ("currency_raw", "currency"), ("payment_term_raw", "payment_term")],
)
def test_supported_legacy_fields_keep_evidence_when_raw_fields_are_missing(raw, canonical):
    source, context = make_input(payment_raw="PP")
    candidate = source.files[0].candidates[0]
    value = getattr(candidate.charge_details, raw)
    setattr(candidate.charge_details, canonical, value)
    setattr(candidate.charge_details, raw, None)
    candidate.field_evidence["/charge_details/" + canonical] = candidate.field_evidence.pop(
        "/charge_details/" + raw
    )
    line = normalize(source, context).lines[0]
    assert line.status == "ready"
    assert line.candidate.field_evidence["/charge_details/" + raw]
    assert line.candidate.field_evidence["/charge_details/" + canonical]
