from copy import deepcopy

import pytest

from shipping_billing.domain.contracts import NormalizationContext
from shipping_billing.domain.engine import normalize
from shipping_billing.domain.evidence import EvidenceIndex
from shipping_billing.extraction.business import enrich_business_context, proof
from shipping_billing.extraction.contracts import (
    EvidenceUnit,
    ExtractedCandidate,
    ExtractionFile,
    ExtractionReport,
)
from shipping_billing.models import ChargeDetails, DocumentInfo, Locator, TransactionParty


def report_for(target="Invoice: INV-10001", support="Invoice: INV-10001", *, parent=None, symbol=None):
    def units(sid, lines):
        return [EvidenceUnit(unit_id=f"{sid}:{i}", source_id=sid, text=line,
                             locator=Locator(kind="email_body", section=f"line-{i}"), method="email_plain")
                for i, line in enumerate(lines.splitlines())]
    own = units("fee", target + "\nType: Invoice\nHandling Fee | 65")
    other = units("support", support)
    candidate = ExtractedCandidate(source_id="fee", source_line_id="fee:line1",
        document_info=DocumentInfo(document_type="Invoice", reference_no="INV-10001",
                                   document_date=None),
        transaction_party=TransactionParty(issuer=None, recipient=None),
        charge_details=ChargeDetails(charge_item="Handling Fee", charge_item_raw="Handling Fee",
                                     amount=65, currency=None, currency_raw=symbol, payment_term=None),
        evidence=[proof(u) for u in own], field_evidence={
            "/document_info/document_type": [proof(own[-2])],
            "/document_info/reference_no": [proof(own[0])],
            "/charge_details/charge_item_raw": [proof(own[-1])],
            "/charge_details/charge_item": [proof(own[-1])],
            "/charge_details/amount": [proof(own[-1])],
        })
    report = ExtractionReport(mode="test_injected", prompt_version="test", input_sha256="test",
        evidence_units=own + other, files=[
            ExtractionFile(source_id="fee", status="extracted", candidates=[candidate]),
            ExtractionFile(source_id="support", parent_source_id=parent, status="no_charge")])
    return report


def enrich(report):
    enrich_business_context(report, {"fee": "fee", "support": "fee" if report.files[1].parent_source_id else "support"})
    return report.files[0].candidates[0]


def test_same_invoice_completes_missing_header_fields_and_domain_rechecks_evidence():
    report = report_for(support="Invoice: INV-10001\nIssuer: Supplier Ltd\nBill to: Buyer Ltd\nCurrency: USD", parent="fee")
    original = deepcopy(report)
    candidate = enrich(report)
    assert len(report.files[0].candidates) == 1 and not report.files[1].candidates
    assert candidate.transaction_party.issuer == "Supplier Ltd"
    assert candidate.transaction_party.recipient == "Buyer Ltd"
    assert candidate.charge_details.currency == "USD" and candidate.charge_details.amount == 65
    result = normalize(report, NormalizationContext(accounting_entity="Buyer Ltd"))
    assert result.lines[0].status == "ready"
    assert result.lines[0].financial_attributes.posting_type == "AP"
    assert any(e.rule == "business.same_document_fill" for e in result.lines[0].events)
    assert all(f.status == "applied" for f in candidate.related_fields)
    assert original.files[0].candidates[0].transaction_party.issuer is None
    assert all(e.source_id == "support" for e in candidate.field_evidence["/transaction_party/issuer"])


def test_explicit_settlement_roles_are_verified_separately_from_invoice_parties():
    report = report_for(support="Invoice: INV-10001\nPAYER: Buyer Ltd\nPAYEE: Supplier Ltd\nCurrency: USD", parent="fee")
    candidate = enrich(report)
    assert candidate.transaction_party.issuer is None
    line = normalize(report, NormalizationContext(accounting_entity="Buyer Ltd")).lines[0]
    assert line.financial_attributes.posting_type == "AP" and line.status == "ready"


@pytest.mark.parametrize("target,support,parent", [
    ("MBL: OOLU1234567", "MBL: OOLU1234567\nPAYER: Buyer Ltd\nPAYEE: Supplier Ltd\nCurrency: USD", None),
    ("Invoice: INV-10001", "Consignee: Buyer Ltd\nBeneficiary: Supplier Ltd\nCurrency: USD", "fee"),
])
def test_shared_shipment_or_email_membership_does_not_automatically_copy_billing(target, support, parent):
    report = report_for(target, support, parent=parent)
    candidate = enrich(report)
    assert candidate.charge_details.currency is None
    assert candidate.transaction_party.issuer is None
    assert not any(f.status == "applied" for f in candidate.related_fields)
    assert normalize(report, NormalizationContext(accounting_entity="Buyer Ltd")).lines[0].status == "pending_review"


def test_shipment_context_offers_payer_payee_and_currency_for_review():
    report = report_for("MBL: OOLU1234567", "MBL: OOLU1234567\nConsignee: Buyer Ltd\nRemit to: Supplier Ltd\nCurrency: USD")
    candidate = enrich(report)
    assert {f.path for f in candidate.related_fields} == {"/settlement/payer", "/settlement/payee", "/charge_details/currency"}
    assert {f.status for f in candidate.related_fields} == {"suggested"}
    assert any(i.code == "RELATED_PARTIES_REQUIRE_CHECK" for i in candidate.issues)


def test_same_mail_different_shipments_do_not_share_candidates():
    report = report_for("MBL: OOLU1234567", "MBL: OOLU9876543\nPAYER: Other Ltd\nCurrency: EUR", parent="fee")
    candidate = enrich(report)
    assert len(report.business_links) == 1 and report.business_links[0].kind == "email_attachment"
    assert not candidate.related_fields


def test_conflicting_related_values_are_not_overwritten_or_automatically_posted():
    report = report_for(support="Invoice: INV-10001\nCurrency: USD\nCurrency: EUR\nIssuer: Supplier Ltd\nBill to: Buyer Ltd")
    candidate = enrich(report)
    assert candidate.charge_details.currency is None
    assert {f.value for f in candidate.related_fields if f.status == "conflict"} == {"USD", "EUR"}
    assert any(i.code == "RELATED_BUSINESS_FIELD_CONFLICT" for i in candidate.issues)
    assert normalize(report, NormalizationContext(accounting_entity="Buyer Ltd")).lines[0].status == "pending_review"


def test_ambiguous_symbol_is_preserved_and_currency_only_suggested():
    report = report_for(support="Invoice: INV-10001\nCurrency: USD", symbol="$")
    candidate = enrich(report)
    assert candidate.charge_details.currency_raw == "$" and candidate.charge_details.currency is None
    assert candidate.related_fields[0].status == "suggested"


def test_multiple_invoice_references_do_not_authorize_cross_source_evidence():
    report = report_for(support="Invoice: INV-10001\nInvoice: INV-10002\nCurrency: USD")
    candidate = enrich(report)
    assert not any(f.status == "applied" for f in candidate.related_fields)
    ref = proof(report.evidence_units[-1])
    assert not EvidenceIndex(report).verify([ref], "USD", "fee")


def test_forged_link_does_not_authorize_foreign_evidence():
    report = report_for(support="Invoice: INV-99999\nCurrency: USD")
    enrich(report)
    # An untrusted link object cannot grant access to another source's facts.
    from shipping_billing.extraction.contracts import BusinessLink
    report.business_links.append(BusinessLink(source_id="fee", related_source_id="support", kind="same_document",
                                               reference_kind="invoice", reference_value="INV-10001"))
    assert not EvidenceIndex(report).verify([proof(report.evidence_units[-1])], "USD", "fee")


def test_existing_direct_values_survive_conflicting_related_material():
    report = report_for(support="Invoice: INV-10001\nBill to: Other Ltd")
    report.files[0].candidates[0].transaction_party.recipient = "Buyer Ltd"
    candidate = enrich(report)
    assert candidate.transaction_party.recipient == "Buyer Ltd"
    assert candidate.related_fields[0].status == "conflict"


def test_shared_invoice_number_alone_does_not_authorize_cross_upload_autofill():
    report = report_for(support="Invoice: INV-10001\nCurrency: USD")
    candidate = enrich(report)
    assert report.business_links[0].kind == "same_reference"
    assert candidate.related_fields[0].status == "suggested"
    assert not EvidenceIndex(report).verify([proof(report.evidence_units[-1])], "USD", "fee")


def test_separate_uploads_can_complement_when_invoice_and_transport_both_match():
    report = report_for("Invoice: INV-10001\nMBL: OOLU1234567",
                        "Invoice: INV-10001\nMBL: OOLU1234567\nCurrency: USD")
    candidate = enrich(report)
    assert candidate.charge_details.currency == "USD"


def test_email_allocation_preserves_party_tokens_as_review_suggestions():
    report = report_for("SHA pay TAR: H/C $50", "MBL: OOLU1234567")
    candidate = enrich(report)
    assert {(f.path, f.value) for f in candidate.related_fields} == {
        ("/settlement/payer", "SHA"), ("/settlement/payee", "TAR")}
    assert all(f.status == "suggested" for f in candidate.related_fields)


def test_uncorroborated_ocr_fields_only_offer_suggestions():
    report = report_for(support="Invoice: INV-10001\nCurrency: USD", parent="fee")
    for unit in report.evidence_units:
        if unit.source_id == "support":
            unit.method = "specified_ocr_text"
    candidate = enrich(report)
    assert candidate.charge_details.currency is None
    assert candidate.related_fields[0].status == "suggested"
