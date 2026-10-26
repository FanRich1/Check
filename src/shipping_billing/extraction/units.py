"""Build small, stable evidence units without dropping pages or truncating input."""

import re

from shipping_billing.extraction.contracts import EvidenceUnit
from shipping_billing.io import dumps


def build_units(report):
    units = []
    for source in report.sources:
        serial = 0

        def add(text, locator, method, metadata=None):
            nonlocal serial
            if not text.strip():
                return
            serial += 1
            units.append(
                EvidenceUnit(
                    unit_id=f"{source.source_id}:u{serial}",
                    source_id=source.source_id,
                    text=text,
                    locator=locator,
                    method=method,
                    metadata=metadata or {},
                )
            )

        for frag in source.fragments:
            # Local OCR boxes are grouped spatially below; avoid duplicate representations.
            if frag.method == "local_ocr_supplement" and source.metadata.get("local_ocr_pages"):
                continue
            for index, line in enumerate(frag.text.splitlines(), 1):
                loc = frag.locator.model_copy(update={"section": f"{frag.fragment_id}:line-{index}"})
                add(line, loc, frag.method)
            # A named correction may put the amount and the fee name on adjacent lines.
            # Preserve their original contiguous paragraph, never concatenate separate fees.
            if frag.method in ("email_plain", "email_html"):
                lines = frag.text.splitlines()
                for index, line in enumerate(lines):
                    if not re.search(r"修改|改为|更改|\b(update|correct|change)\b", line, re.I):
                        continue
                    if index + 1 >= len(lines) or not re.match(
                        r"\s*(?:费用名称|fee name)\s*[:： ]", lines[index + 1], re.I
                    ):
                        continue
                    end = index + 2
                    if end < len(lines) and re.match(r"\s*(?:币种|currency)\b|\s*币种", lines[end], re.I):
                        end += 1
                    paragraph = "\n".join(lines[index:end])
                    if len(paragraph) <= 1500:
                        loc = frag.locator.model_copy(
                            update={"section": f"{frag.fragment_id}:edit-paragraph-{index + 1}"}
                        )
                        add(paragraph, loc, frag.method, {"original_contiguous_lines": [index + 1, end]})
        for page in source.metadata.get("local_ocr_pages", []):
            # Separate address columns before grouping horizontal table rows.
            # Coordinates, not company-specific names, define these panels.
            anchors = [b for b in page["boxes"] if re.search(
                r"all payments.*(?:issued|made)|remit\s+to|payment\s+to", b["text"], re.I
            )]
            for anchor in anchors:
                split = min(p[0] for p in anchor["polygon"])
                start = min(p[1] for p in anchor["polygon"])
                height = max(p[1] for p in anchor["polygon"]) - start
                nearby, bottom = [], start
                for box in sorted(page["boxes"], key=lambda b: min(p[1] for p in b["polygon"])):
                    top = min(p[1] for p in box["polygon"])
                    if top < start - height:
                        continue
                    if top - bottom > max(24, height * 3):
                        break
                    nearby.append(box)
                    bottom = max(bottom, max(p[1] for p in box["polygon"]))
                for side in ("left", "right"):
                    boxes = [b for b in nearby if (min(p[0] for p in b["polygon"]) < split - height) == (side == "left")]
                    if not boxes:
                        continue
                    from shipping_billing.models import Locator
                    add("\n".join(b["text"] for b in boxes),
                        Locator(kind="image_region" if source.detected_format != "pdf" else "pdf_page",
                                page=page["page"], region=f"address_panel:{side}:y={start}"),
                        "local_ocr_address_panel", {"boxes": boxes, "reading_order": "top_to_bottom",
                        "panel": side, "anchor": anchor["text"]})
            groups = []
            for box in sorted(page["boxes"], key=lambda b: sum(p[1] for p in b["polygon"]) / 4):
                y = sum(p[1] for p in box["polygon"]) / 4
                group = next((g for g in groups if abs(g[0] - y) <= 6), None)
                if group is None:
                    group = [y, []]
                    groups.append(group)
                group[1].append(box)
            from shipping_billing.models import Locator

            for _, boxes in groups:
                text = " ".join(
                    b["text"].strip() for b in sorted(boxes, key=lambda b: min(p[0] for p in b["polygon"]))
                )
                points = [p for box in boxes for p in box["polygon"]]
                bbox = [
                    min(p[0] for p in points),
                    min(p[1] for p in points),
                    max(p[0] for p in points),
                    max(p[1] for p in points),
                ]
                add(
                    text,
                    Locator(
                        kind="image_region" if source.detected_format != "pdf" else "pdf_page",
                        page=page["page"],
                        region="bbox_pixels=" + dumps(bbox),
                    ),
                    "local_ocr_spatial_row",
                    {"boxes": boxes},
                )
        # Native Office paragraphs can split a table into cells: include a row view as context.
        if source.detected_format in ("doc", "docx"):
            for table in source.tables:
                for index, row in enumerate(table.rows, 1):
                    loc = table.locator.model_copy(update={"section": f"{table.table_id}:row-{index}"})
                    add("\t".join("" if x is None else str(x) for x in row), loc, "word_table_row")
    return units
