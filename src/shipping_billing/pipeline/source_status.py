"""Separate empty image content requiring human review from execution failures."""

PURPOSE_ONLY = {
    "OCR_EMPTY_CONTENT", "OCR_STRUCTURED_REQUIRES_CHECK", "OCR_NO_ORIGINAL_TEXT",
    "IMAGE_TEXT_REQUIRES_VERIFICATION", "LOCAL_OCR_EMPTY", "LOCAL_OCR_LOW_CONFIDENCE",
    "NO_SOURCE_TEXT", "CHILD_REQUIRES_REVIEW", "IMAGE_OCR_EMPTY_REQUIRES_PURPOSE_CONFIRMATION",
}
IMAGES = {"png", "jpg", "tiff"}
PURPOSE_REQUIRED = "IMAGE_OCR_EMPTY_REQUIRES_PURPOSE_CONFIRMATION"


def empty_image_requires_confirmation(source, *, has_fees, issue_codes):
    """An empty OCR result never proves a picture is non-billing material."""
    if (source.get("detected_format") not in IMAGES or source.get("status") == "error"
            or has_fees or set(issue_codes) - PURPOSE_ONLY):
        return False
    metadata = source.get("metadata", {})
    frames = metadata.get("frames", 0)
    pages = metadata.get("ocr_page_status", [])
    if (not frames or len(pages) != frames
            or {p.get("page") for p in pages} != set(range(1, frames + 1))):
        return False
    empty = [p for p in pages if p.get("status") == "error" and p.get("code") == "OCR_EMPTY_CONTENT"
             and not p.get("outcome_unknown")]
    return bool(empty) and all(p.get("status") == "completed" or p in empty for p in pages)
