"""Evaluate retained source evidence against the pre-existing local visual baseline.

This does not score financial decisions or model accuracy; labels never enter parsing.
"""

import argparse
import re
from decimal import Decimal
from pathlib import Path

from shipping_billing.io import load_json, write_json


def compact(text):
    return re.sub(r"\s+", "", text).upper()


def amounts(text):
    return [Decimal(x.replace(",", "")) for x in re.findall(r"[+-]?\d[\d,]*(?:\.\d+)?", text)]


def text_rows(source):
    rows = []
    for fragment in source["fragments"]:
        if fragment["method"] in ("pdf_native_text", "email_plain", "email_html"):
            rows.extend(fragment["text"].splitlines())
    for page in source["metadata"].get("local_ocr_pages", []):
        grouped = []
        for box in sorted(page["boxes"], key=lambda b: sum(p[1] for p in b["polygon"]) / 4):
            center = sum(p[1] for p in box["polygon"]) / 4
            group = next((g for g in grouped if abs(g[0] - center) <= 6), None)
            if group is None:
                group = [center, []]
                grouped.append(group)
            group[1].append(box)
        rows += [
            " ".join(b["text"] for b in sorted(g[1], key=lambda b: min(p[0] for p in b["polygon"])))
            for g in grouped
        ]
    return rows


def evaluate(report, root):
    manifest = load_json(root / "data/local/stage0/source-manifest.json")
    golden = load_json(root / "data/local/stage0/golden-real.json")
    sources = report["sources"]
    source_by_id = {s["source_id"]: s for s in sources}
    inventory = []
    for expected in manifest["sources"]:
        matches = [s for s in sources if s["sha256"] == expected["sha256"]]
        inventory.append(
            {
                "source_id": expected["source_id"],
                "retained": bool(matches),
                "parent_link_correct": bool(matches)
                and matches[0]["parent_source_id"] == expected["parent_id"],
            }
        )
    fee_checks = []
    anchor_checks = []
    for case in golden["cases"]:
        source = source_by_id[case["source_ids"][0]]
        rows = text_rows(source)
        text = "\n".join(f["text"] for f in source["fragments"] if f["method"] != "specified_ocr_text")
        available = set(range(len(rows)))
        for fee in case["fees"]:
            charge = fee["charge_details"]
            hit = next(
                (
                    i
                    for i in sorted(available)
                    if compact(charge["charge_item_raw"]) in compact(rows[i])
                    and charge["amount"] in amounts(rows[i])
                ),
                None,
            )
            if hit is not None:
                available.remove(hit)
            fee_checks.append(
                {
                    "source_id": source["source_id"],
                    "line_id": fee["line_id"],
                    "retained": hit is not None,
                    "retained_row": rows[hit] if hit is not None else None,
                }
            )
        anchors = {}
        for key in ("reference_no", "document_date_raw"):
            if case["document_info"].get(key):
                anchors[f"document_info/{key}"] = case["document_info"][key]
        for key, value in case.get("transaction_party", {}).items():
            if value:
                anchors[f"transaction_party/{key}"] = value
        for key, value in case.get("shipping_info", {}).items():
            if isinstance(value, str) and value:
                anchors[f"shipping_info/{key}"] = value
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str) and item:
                        anchors[f"shipping_info/{key}/{index}"] = item
        if case.get("document_facts", {}).get("currency"):
            anchors["document_facts/currency"] = case["document_facts"]["currency"]
        for path, value in anchors.items():
            anchor_checks.append(
                {"source_id": source["source_id"], "path": path, "retained": compact(value) in compact(text)}
            )
    formats = {
        kind: any(s["detected_format"] == kind and s["status"] != "error" for s in sources)
        for kind in ("eml", "pdf", "png", "jpg", "tiff", "xls", "xlsx", "doc", "docx")
    }
    pages = [(s, p) for s in sources for p in s["metadata"].get("page_inventory", [])]
    located = all(f["source_id"] == s["source_id"] and f["locator"] for s in sources for f in s["fragments"])
    return {
        "scope": "source_evidence_retention_not_billing_accuracy",
        "independent_human_signoff": False,
        "formats": formats,
        "inventory": inventory,
        "fee_rows": fee_checks,
        "text_anchors": anchor_checks,
        "inventory_retained": {
            "numerator": sum(x["retained"] and x["parent_link_correct"] for x in inventory),
            "denominator": len(inventory),
        },
        "fee_rows_retained": {
            "numerator": sum(x["retained"] for x in fee_checks),
            "denominator": len(fee_checks),
        },
        "text_anchors_retained": {
            "numerator": sum(x["retained"] for x in anchor_checks),
            "denominator": len(anchor_checks),
        },
        "page_inventory_count": len(pages),
        "fragment_sources_located": located,
        "errors": [s["source_id"] for s in sources if s["status"] == "error"],
        "retention_passed": all(formats.values())
        and all(x["retained"] and x["parent_link_correct"] for x in inventory)
        and all(x["retained"] for x in fee_checks + anchor_checks)
        and located
        and not any(s["status"] == "error" for s in sources),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.input.resolve() == args.output.resolve():
        raise SystemExit("Output must not overwrite input")
    report = evaluate(load_json(args.input), Path(__file__).resolve().parents[1])
    write_json(args.output, report)
    for key in ("inventory_retained", "fee_rows_retained", "text_anchors_retained", "retention_passed"):
        print(key, report[key])
    raise SystemExit(0 if report["retention_passed"] else 1)


if __name__ == "__main__":
    main()
