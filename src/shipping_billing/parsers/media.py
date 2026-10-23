"""Page-aware PDF and image evidence. Vendor fields remain distinct from original text."""

import io
import re
import warnings
from collections import Counter

import pdfplumber
from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

from shipping_billing.errors import BillingError
from shipping_billing.models import Locator
from shipping_billing.parsers.contracts import ParsedTable


def check_vendor_against_text(source, page):
    fragments = [
        f.text
        for f in source.fragments
        if f.locator.page == page and f.method in ("pdf_native_text", "local_ocr_supplement")
    ]
    text = "\n".join(fragments)
    if not text.strip():
        return
    compact = re.sub(r"\s+", "", text).upper()
    for result in source.ocr_results:
        if result["source_page"] != page:
            continue
        for doc in result.get("documents", []):
            facts = doc.get("vendor_facts", {})
            ids = [facts.get("invoiceNumber"), facts.get("mblNumber")]
            ids += [c.get("ctrNumber") for c in facts.get("ctrList", []) if isinstance(c, dict)]
            if any(
                isinstance(value, str) and value.strip() and re.sub(r"\s+", "", value).upper() not in compact
                for value in ids
            ):
                source.issue(
                    "OCR_IDENTIFIER_NOT_CORROBORATED",
                    "A vendor identifier is not corroborated by native or supplementary text; retain both versions for review.",
                )
            counts = Counter(
                str(c.get("chargeItem", "")).strip().upper()
                for c in facts.get("chargeList", [])
                if isinstance(c, dict)
            )
            for name, count in counts.items():
                if not name:
                    continue
                occurrences = sum(
                    bool(
                        re.match(
                            r"^\s*" + re.escape(name) + r"(?:\s*$|\s+(?:[-|$]|\d|USD\b|CNY\b|EUR\b))",
                            line.upper(),
                        )
                    )
                    for line in text.splitlines()
                )
                if occurrences > count:
                    source.issue(
                        "OCR_POSSIBLE_MERGED_FEE_ROWS",
                        "Source text contains more separate same-name lines than the vendor fee list; do not accept merged charges.",
                    )


def add_local_text(source, image, page, local_ocr, *, pdf_scale=None):
    if local_ocr is None:
        return
    boxes = local_ocr(image)
    source.metadata.setdefault("local_ocr_pages", []).append(
        {"page": page, "boxes": boxes, "engine": local_ocr.metadata, "pdf_render_scale": pdf_scale}
    )
    if not boxes:
        source.issue("LOCAL_OCR_EMPTY", "Supplementary local OCR found no text.")
    for row in boxes:
        region = "polygon_pixels=" + str(row["polygon"])
        locator = (
            Locator(kind="pdf_page", page=page, region=region)
            if source.detected_format == "pdf"
            else Locator(kind="image_region", page=page, region=region)
        )
        source.fragment(row["text"], locator, "local_ocr_supplement")
        if row["confidence"] < 0.8:
            source.issue(
                "LOCAL_OCR_LOW_CONFIDENCE", "Some supplementary OCR text has low recognition confidence."
            )


def add_ocr(source, content, content_type, page, provider):
    locator = (
        Locator(kind="pdf_page", page=page)
        if source.detected_format == "pdf"
        else Locator(kind="image_region", page=page, region=f"frame-{page}:full-image")
    )
    if provider is None:
        source.metadata.setdefault("ocr_page_status", []).append(
            {"page": page, "status": "not_run", "mode": "offline"}
        )
        source.issue(
            "OCR_NOT_RUN", "Offline parse preserves local evidence; specified OCR has not run for this page."
        )
        return
    try:
        result = provider(content, source.source_id, page, content_type)
        source.metadata.setdefault("ocr_page_status", []).append(
            {"page": page, "status": "completed", "mode": result.get("mode")}
        )
        source.ocr_results.append({"source_page": page, **result})
        for text in result.get("text_blocks", []):
            source.fragment(text, locator, "specified_ocr_text")
        if result.get("documents"):
            source.issue(
                "OCR_STRUCTURED_REQUIRES_CHECK",
                "Vendor fields may merge rows or misread identifiers; verify against source evidence.",
            )
        if not result.get("text_blocks"):
            source.issue(
                "OCR_NO_ORIGINAL_TEXT",
                "Specified OCR did not provide page text; structured fields are not original quotations.",
            )
    except BillingError as exc:
        source.metadata.setdefault("ocr_page_status", []).append(
            {
                "page": page,
                "status": "error",
                "code": exc.code,
                "outcome_unknown": exc.code == "OCR_TIMEOUT_UNKNOWN_OUTCOME",
            }
        )
        source.issue(exc.code, exc.message)


def parse_pdf(data, source, limits, provider, local_ocr=None):
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        raise BillingError("ENCRYPTED_FILE", "Encrypted PDFs require manual handling.")
    if len(reader.pages) > limits.max_pages:
        raise BillingError("PAGE_LIMIT", "PDF exceeds configured page limit.")
    source.metadata["pages"] = len(reader.pages)
    source.metadata["page_inventory"] = []
    with pdfplumber.open(io.BytesIO(data)) as doc:
        for number, page in enumerate(doc.pages, 1):
            text = page.extract_text(layout=False) or ""
            source.fragment(text, Locator(kind="pdf_page", page=number), "pdf_native_text")
            source.metadata["page_inventory"].append(
                {
                    "page": number,
                    "native_text_present": bool(text.strip()),
                    "width": float(page.width),
                    "height": float(page.height),
                }
            )
            source.metadata["page_inventory"][-1]["words"] = page.extract_words()
            if not text.strip():
                source.issue(
                    "PDF_NO_NATIVE_TEXT", "Page has no extractable text; requires image recognition."
                )
                if local_ocr:
                    import pypdfium2 as pdfium

                    with pdfium.PdfDocument(data) as raster:
                        raster_page = raster[number - 1]
                        scale = min(
                            2.0, (limits.max_image_pixels / (float(page.width) * float(page.height))) ** 0.5
                        )
                        bitmap = raster_page.render(scale=scale)
                        try:
                            add_local_text(source, bitmap.to_pil(), number, local_ocr, pdf_scale=scale)
                        finally:
                            bitmap.close()
                            raster_page.close()
            for index, table in enumerate(page.find_tables(), 1):
                rows = table.extract()
                if sum(len(row) for row in rows) > limits.max_cells:
                    raise BillingError("CELL_LIMIT", "PDF table exceeds cell limit.")
                source.tables.append(
                    ParsedTable(
                        table_id=f"{source.source_id}:p{number}:t{index}",
                        locator=Locator(kind="pdf_page", page=number),
                        rows=rows,
                        metadata={"bbox": list(table.bbox), "method": "pdf_native_lines"},
                    )
                )
            if provider is None:
                add_ocr(source, data, "application/pdf", number, None)
            else:
                # The single-page derivative establishes source page identity independently of vendor page_index.
                writer = PdfWriter()
                writer.add_page(reader.pages[number - 1])
                buffer = io.BytesIO()
                writer.write(buffer)
                add_ocr(source, buffer.getvalue(), "application/pdf", number, provider)
            check_vendor_against_text(source, number)


def parse_image(data, source, limits, provider, local_ocr=None):
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > limits.max_pages:
                raise BillingError("PAGE_LIMIT", "Image exceeds frame limit.")
            source.metadata["frames"] = frames
            source.metadata["page_inventory"] = []
            for index in range(frames):
                image.seek(index)
                if image.width * image.height > limits.max_image_pixels:
                    raise BillingError("IMAGE_PIXEL_LIMIT", "Image exceeds pixel limit.")
                image.load()
                frame = ImageOps.exif_transpose(image.copy())
                source.metadata["page_inventory"].append(
                    {
                        "page": index + 1,
                        "width": frame.width,
                        "height": frame.height,
                        "pixel_space": "exif_transposed",
                        "original_exif_orientation": image.getexif().get(274, 1),
                    }
                )
                if (
                    source.detected_format in ("png", "jpg")
                    and frames == 1
                    and image.getexif().get(274, 1) == 1
                ):
                    encoded = data
                    content_type = "image/png" if source.detected_format == "png" else "image/jpeg"
                else:
                    buffer = io.BytesIO()
                    frame.convert("RGB").save(buffer, format="PNG")
                    encoded, content_type = buffer.getvalue(), "image/png"
                add_ocr(source, encoded, content_type, index + 1, provider)
                add_local_text(source, frame, index + 1, local_ocr)
                check_vendor_against_text(source, index + 1)
                source.issue(
                    "IMAGE_TEXT_REQUIRES_VERIFICATION",
                    "Image text and business identifiers need verified recognition; image retained as source.",
                )
    text = "\n".join(f.text for f in source.fragments).upper()
    if any(term in text for term in ("INVOICE", "DEBIT NOTE", "CREDIT NOTE", "账单")):
        category = "document_image_candidate"
    elif text.strip() and not re.search(r"\d+[.,]\d{2}|USD|CNY|EUR|\$", text):
        category = "supporting_graphic_candidate"
    else:
        category = "unclassified_image"
    source.metadata["content_classification"] = {
        "category": category,
        "rule": "text-cues-v1",
        "discarded": False,
    }
