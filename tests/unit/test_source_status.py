from copy import deepcopy

import pytest

from shipping_billing.pipeline.source_status import empty_image_requires_confirmation
from shipping_billing.web.service import processing_summary
from shipping_billing.web.source_usage import assess_sources


@pytest.mark.parametrize("case,expected", [
    ("empty_image", True), ("pdf", False), ("fees", False), ("read_error", False),
    ("timeout", False), ("missing_page", False), ("duplicate_page", False),
    ("unknown_outcome", False), ("model_error", False), ("mixed_frames", True),
])
def test_empty_content_is_pending_only_when_image_reading_is_complete(case, expected):
    source = dict(source_id="s", detected_format="jpg", status="pending_review",
                  metadata=dict(frames=1, ocr_page_status=[dict(page=1, status="error", code="OCR_EMPTY_CONTENT")]))
    codes = {"OCR_EMPTY_CONTENT"}
    if case == "pdf":
        source["detected_format"] = "pdf"
    if case == "read_error":
        source["status"] = "error"
    if case == "timeout":
        source["metadata"]["ocr_page_status"][0]["code"] = "OCR_TIMEOUT"
    if case in {"missing_page", "duplicate_page", "mixed_frames"}:
        source["metadata"]["frames"] = 2
    if case == "duplicate_page":
        source["metadata"]["ocr_page_status"] *= 2
    if case == "mixed_frames":
        source["metadata"]["ocr_page_status"].append(dict(page=2, status="completed"))
    if case == "unknown_outcome":
        source["metadata"]["ocr_page_status"][0]["outcome_unknown"] = True
    if case == "model_error":
        codes.add("MODEL_INVALID_STRUCTURE")
    before = deepcopy(source)
    assert empty_image_requires_confirmation(source, has_fees=case == "fees", issue_codes=codes) == expected
    assert source == before
    file = dict(source_id="s", status="error", detected_format=source["detected_format"], record_ids=[],
                issues=[dict(code=c, blocks_automation=True) for c in codes])
    facts = dict(source_id="s", status="pending_review", candidates=[{}] if case == "fees" else [])
    assessment = assess_sources([file], dict(sources=[source]), dict(files=[facts]), [], {}, {})[0]["assessment"]
    assert assessment["status"] == ("purpose_pending" if expected else "error")
    assert assessment["can_confirm"] == expected


def test_raw_batch_summary_reports_confirmation_instead_of_no_fees_or_failure():
    result = processing_summary(dict(counts=dict(fee_lines=0, PENDING_REVIEW=0, purpose_pending_sources=1),
                                     files=[dict(status="pending_review")]))
    assert result["source_errors"] == 0 and result["purpose_pending_count"] == 1
    assert result["fee_message"] == "尚未提取到费用"
