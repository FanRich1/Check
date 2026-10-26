from copy import deepcopy

import pytest

from shipping_billing.web.source_usage import assess_sources, native_transport_support

TEXT = "BILL OF LADING\nSHIPPER: Sender\nCONSIGNEE: Receiver\nFREIGHT PREPAID\n10715.000 KGS\nB/L NUMBER: BL1234567"


def source(text=TEXT):
    return dict(source_id="s", detected_format="pdf", status="pending_review",
                fragments=[dict(text=text, method="pdf_native_text", locator=dict(kind="pdf_page", page=1))],
                metadata=dict(pages=1, ocr_page_status=[dict(page=1, status="completed")]))


def file(kind="pdf", error="OCR_NO_ORIGINAL_TEXT"):
    return dict(source_id="s", detected_format=kind, parent_source_id="mail", sha256="a" * 64,
                status="error" if error == "OCR_EMPTY_CONTENT" else "pending_review", record_ids=[],
                issues=[dict(code=error, blocks_automation=True)])


@pytest.mark.parametrize("text,recognized", [
    (TEXT, True),
    (TEXT.replace("FREIGHT PREPAID", "AS ARRANGEED"), True),
    (TEXT + "\nUSD 50.00", False),
    (TEXT + "\n50.00 USD", False),
    (TEXT + "\n$50", False),
    (TEXT + "\n50.00", False),
    (TEXT + "\nINVOICE", False),
    ("Nothing to bill", False),
])
def test_supporting_transport_needs_explicit_evidence_and_no_price(text, recognized):
    assert bool(native_transport_support(source(text), dict(candidates=[], status="no_charge"))) == recognized


def test_incomplete_page_or_ocr_does_not_become_transport_support():
    parsed = source()
    parsed["metadata"]["pages"] = 2
    assert not native_transport_support(parsed, dict(candidates=[], status="no_charge"))
    parsed = source()
    parsed["metadata"]["ocr_page_status"][0]["status"] = "error"
    assert not native_transport_support(parsed, dict(candidates=[], status="no_charge"))
    assert not native_transport_support(source(), dict(candidates=[{}], status="extracted"))


def test_empty_ocr_image_requires_explicit_purpose_confirmation_and_preserves_history():
    original = file("jpg", "OCR_EMPTY_CONTENT")
    snapshot = deepcopy(original)
    parsed = dict(sources=[dict(source_id="s", detected_format="jpg", status="pending_review",
                               metadata=dict(frames=1, ocr_page_status=[dict(
                                   page=1, status="error", code="OCR_EMPTY_CONTENT")]))])
    extraction = dict(files=[dict(source_id="s", candidates=[], status="pending_review")])
    result = assess_sources([original], parsed, extraction, [], {}, {})[0]
    assert result["assessment"]["status"] == "purpose_pending"
    review = dict(revision=1, classification="non_billing_image", sha256="a" * 64,
                  reviewer="Reviewer", reason="Viewed original promotional banner")
    confirmed = assess_sources([original], parsed, extraction, [], {}, {"s": [review]})[0]
    assert confirmed["assessment"]["status"] == "non_billing"
    assert confirmed["issues"] == snapshot["issues"] and original == snapshot
    reopened = {**review, "revision": 2, "classification": "reopen"}
    assert assess_sources([original], parsed, extraction, [], {}, {"s": [review, reopened]})[0]["assessment"]["status"] == "purpose_pending"


def test_real_read_failure_is_not_hidden_by_a_purpose_record():
    original = file("jpg", "MISSING_OCR_DELIVERY")
    original["status"] = "error"
    parsed = dict(sources=[dict(source_id="s", status="pending_review")])
    extraction = dict(files=[dict(source_id="s", candidates=[], status="error")])
    review = dict(revision=1, classification="non_billing_image", sha256="a" * 64, reason="Claimed banner")
    use = assess_sources([original], parsed, extraction, [], {}, {"s": [review]})[0]["assessment"]
    assert use["status"] == "error" and not use["can_confirm"]


def test_existing_fees_cannot_be_hidden_by_classifying_the_source_as_non_billing():
    original = file("jpg", "OCR_EMPTY_CONTENT")
    original["record_ids"] = ["fee1"]
    extraction = dict(files=[dict(source_id="s", candidates=[{}], status="pending_review")])
    review = dict(revision=1, classification="non_billing_image", sha256="a" * 64, reason="Incorrect classification")
    use = assess_sources([original], dict(sources=[]), extraction,
                         [dict(record_id="fee1", operation="PENDING_REVIEW")], {}, {"s": [review]})[0]["assessment"]
    assert use["status"] == "fee_pending" and not use["can_confirm"]
