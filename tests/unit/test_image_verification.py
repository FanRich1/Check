from copy import deepcopy

import pytest

from shipping_billing.extraction.contracts import ExtractionDraft, ExtractionFile, Observation
from shipping_billing.extraction.engine import materialize
from shipping_billing.extraction.image_verification import verify_image_evidence
from shipping_billing.extraction.units import build_units
from shipping_billing.models import Locator
from shipping_billing.parsers.contracts import ParsedSource, ParseReport


def sample():
    def box(text, x, y, width=100, score=.99):
        return dict(text=text, confidence=score, polygon=[[x, y], [x+width, y], [x+width, y+8], [x, y+8]])
    boxes = [box("Buyer Ltd", 20, 100), box("All payments must be issued to:", 300, 100, 220),
             box("Payer: CODE123", 20, 112), box("Seller Ltd", 300, 112),
             box("INVOICE REF-001", 20, 250), box("Handling 5.00 USD", 20, 400),
             box("Storage 21.90 USD", 20, 420), box("USD 26.90", 20, 460)]
    source = ParsedSource(source_id="s", original_name="invoice.png", detected_format="png",
                          sha256="a"*64, size_bytes=100)
    source.metadata = {"frames": 1, "local_ocr_pages": [{"page": 1, "boxes": boxes}],
                       "ocr_page_status": [{"page": 1, "status": "completed"}]}
    source.ocr_results = [{"documents": [{"vendor_facts": {"invoiceNumber": "REF-001",
        "chargeList": [{"chargeItem": "Handling", "amount": "5.00"},
                       {"chargeItem": "Storage", "amount": "21.90"}]}}]}]
    for b in boxes:
        source.fragment(b["text"], Locator(kind="image_region", page=1, region=str(b["polygon"])), "local_ocr_supplement")
    for code in ("IMAGE_TEXT_REQUIRES_VERIFICATION", "OCR_NO_ORIGINAL_TEXT", "OCR_STRUCTURED_REQUIRES_CHECK"):
        source.issue(code, "needs verification")
    units = {u.unit_id:u for u in build_units(ParseReport(mode="replay", sources=[source]))}
    def cite(text, side=None):
        unit = next(u for u in units.values() if text in u.text and (
            u.metadata.get("panel") == side if side else u.method == "local_ocr_spatial_row"))
        return [{"unit_id": unit.unit_id, "quote": text}]
    draft = ExtractionDraft.model_validate({"documents": [{"document_type": "INVOICE",
        "reference_no": "REF-001", "document_date_raw": None, "issuer": "Seller Ltd",
        "recipient": "Buyer Ltd", "shipping_info": {}, "evidence": {
            "document_type": cite("INVOICE"), "reference_no": cite("REF-001"),
            "issuer": cite("Seller Ltd", "right"), "recipient": cite("Buyer Ltd", "left")},
        "fees": [{"charge_item_raw": name, "amount": amount, "currency_raw": "USD",
                  "quantity": None, "unit_price": None, "payment_term_raw": None,
                  "evidence": {k: cite(f"{name} {amount:.2f} USD")
                               for k in ("charge_item_raw", "amount", "currency_raw")}}
                 for name, amount in [("Handling", 5), ("Storage", 21.90)]]}],
        "observations": []})
    candidates, problems = materialize(draft, source, units, {"s"})
    file = ExtractionFile(source_id="s", status="pending_review", candidates=candidates,
                          issues=source.issues+problems,
                          observations=[Observation(kind="total", citations=cite("USD 26.90"))])
    return source, file, units


@pytest.mark.parametrize("fault", [None, "amount", "identifier", "total", "missing_total", "low_confidence",
                                  "missing_role", "failed_ocr", "unverified_field", "merged_rows"])
def test_image_admission_requires_independent_evidence(fault):
    source, file, units = sample()
    facts = source.ocr_results[0]["documents"][0]["vendor_facts"]
    if fault == "amount":
        facts["chargeList"][0]["amount"] = "50.00"
    elif fault == "identifier":
        facts["invoiceNumber"] = "REF-009"
    elif fault == "total":
        file.observations[0].citations[0].quote = "USD 99.00"
    elif fault == "missing_total":
        file.observations = []
    elif fault == "low_confidence":
        for u in units.values():
            for b in u.metadata.get("boxes", []):
                if "Buyer" in b["text"]:
                    b["confidence"] = .6
    elif fault == "missing_role":
        file.candidates[0].transaction_party.recipient = None
    elif fault == "failed_ocr":
        source.metadata["ocr_page_status"][0]["status"] = "error"
    elif fault == "unverified_field":
        file.candidates[0].field_evidence["/charge_details/amount"][0].quote = "500.00"
    elif fault == "merged_rows":
        facts["chargeList"] = facts["chargeList"][:1]
    original = deepcopy(source.issues)
    verify_image_evidence(source, file, units)
    assert any(i.code == "IMAGE_TEXT_VERIFIED" for i in file.issues) == (fault is None)
    assert source.issues == original


def test_address_panels_keep_customer_and_remittance_separate():
    _, _, units = sample()
    panels = {u.metadata["panel"]:u.text for u in units.values() if u.method == "local_ocr_address_panel"}
    assert "Buyer Ltd" in panels["left"] and "Seller Ltd" not in panels["left"]
    assert "Seller Ltd" in panels["right"] and "CODE123" not in panels["right"]
