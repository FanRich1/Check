"""Bounded image admission using independent fee agreement and spatial evidence."""

import re
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext

from shipping_billing.models import Issue


def compact(value):
    return re.sub(r"\s+", "", str(value)).upper()


def verify_image_evidence(source, file, units):
    generic = {"OCR_STRUCTURED_REQUIRES_CHECK", "OCR_NO_ORIGINAL_TEXT",
               "LOCAL_OCR_LOW_CONFIDENCE", "IMAGE_TEXT_REQUIRES_VERIFICATION"}
    if source.detected_format not in ("png", "jpg", "tiff") or source.metadata.get("frames") != 1:
        return
    if not file.candidates or source.metadata.get("ocr_page_status") is None:
        return
    if not all(p["status"] == "completed" for p in source.metadata["ocr_page_status"]):
        return
    if not any(p["page"] == 1 for p in source.metadata["ocr_page_status"]):
        return
    issues = file.issues + [i for c in file.candidates for i in c.issues]
    if any(i.blocks_automation and i.code not in generic for i in issues):
        return
    native_units = [u for u in units.values() if u.source_id == source.source_id]

    def high_confidence(ref):
        # Low-confidence logos/base codes do not invalidate unrelated amount boxes.
        for unit in native_units:
            if unit.locator != ref.locator or unit.source_id != ref.source_id:
                continue
            boxes = unit.metadata.get("boxes", [])
            if unit.method == "local_ocr_spatial_row":
                boxes = sorted(boxes, key=lambda b: min(p[0] for p in b["polygon"]))
            text = " ".join(b["text"] if b["confidence"] >= .9 else "\ufffd" for b in boxes)
            if boxes and compact(ref.quote) in compact(text):
                return True
        return False

    def role_panel(candidate, role, side):
        refs = candidate.field_evidence.get("/transaction_party/" + role, [])
        return refs and all(any(u.locator == ref.locator and u.source_id == ref.source_id
            and u.method == "local_ocr_address_panel" and u.metadata.get("panel") == side
            for u in native_units) for ref in refs)

    for c in file.candidates:
        if not c.document_info.reference_no or not c.transaction_party.issuer or not c.transaction_party.recipient:
            return
        if not role_panel(c, "issuer", "right") or not role_panel(c, "recipient", "left"):
            return
        if not c.field_evidence or any(not refs or not all(high_confidence(r) for r in refs)
                                       for refs in c.field_evidence.values()):
            return
    references = {compact(c.document_info.reference_no) for c in file.candidates}
    currencies = {c.charge_details.currency for c in file.candidates}
    if len(references) != 1 or len(currencies) != 1 or None in currencies:
        return
    facts = [d.get("vendor_facts", {}) for r in source.ocr_results for d in r.get("documents", [])]
    if {compact(f["invoiceNumber"]) for f in facts if f.get("invoiceNumber")} != references:
        return
    try:
        vendor = Counter((compact(c["chargeItem"]), Decimal(str(c["amount"])))
                         for f in facts for c in f.get("chargeList", []))
    except (KeyError, InvalidOperation, TypeError):
        return
    actual = Counter((compact(c.charge_details.charge_item_raw), c.charge_details.amount) for c in file.candidates)
    if vendor != actual or any(amount is None or not amount.is_finite() for _, amount in actual):
        return
    with localcontext() as ctx:
        ctx.prec = 400
        total = sum((c.charge_details.amount for c in file.candidates), Decimal(0))
    currency = next(iter(currencies))
    totals = []
    for observation in file.observations:
        if observation.kind != "total":
            continue
        for ref in observation.citations:
            unit = units.get(ref.unit_id)
            if not unit or unit.source_id != source.source_id:
                return
            from shipping_billing.models import Evidence
            if not high_confidence(Evidence(source_id=unit.source_id, locator=unit.locator, quote=ref.quote)):
                return
            match = re.fullmatch(r"\s*(?:TOTAL\s*[:：]?\s*)?" + re.escape(currency)
                                 + r"\s+([\d,]+\.\d{2})\s*", ref.quote, re.I)
            if not match:
                return
            totals.append(Decimal(match[1].replace(",", "")))
    if not totals or any(t != total for t in totals):
        return
    proof = Issue(code="IMAGE_TEXT_VERIFIED", severity="info", blocks_automation=False,
                  message="Spatial parties and fields verified; independent OCR fees/identifiers agree and total balances.")

    def verified(items):
        return [i.model_copy(update={"blocks_automation": False}) if i.code in generic else i for i in items] + [proof]
    file.issues = verified(file.issues)
    for c in file.candidates:
        c.issues = verified(c.issues)
