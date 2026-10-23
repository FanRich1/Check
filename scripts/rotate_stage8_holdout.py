"""Consume the failed v1 holdout, author a new layout family before inference, retain all evidence."""

import hashlib
from decimal import Decimal

from scripts import prepare_stage8_cases as author
from shipping_billing.io import load_json, write_json

BASE = author.BASE


def panel_lines(config, rows, split):
    """New description-first invoice panels; independent of prior slash/item layouts."""
    lines = [
        config["type"],
        f"Supplier / issuer: {config['issuer']}",
        f"Customer / bill to: {config['recipient']}",
        f"Invoice No: {config['reference']} | Invoice date: {config.get('date', '2026-10-22')}",
        "REGISTER OF COMPLETED SERVICES",
        "Each entry below represents a separate service delivered.",
    ]
    for key, value in config.get("shipping", {}).items():
        lines.append(f"{key}: {value}")
    for n, row in enumerate(rows, 1):
        row.setdefault("marker", f"ENTRY-{n:02}")
        line = f"Description: {row['raw']} | Amount: {row['amount']} {row['currency']} | Terms: {row['payment']} | Entry: {row['marker']}"
        if row["quantity"] is not None:
            line += f" | Quantity: {row['quantity']} | Unit price: {row['unit_price']}"
        lines.append(line)
    return lines + config.get("notes", [])


def main():
    manifest_path = BASE / "manifest-holdout.json"
    previous = load_json(manifest_path)
    if len(previous["cases"]) != 15 or previous["cases"][0]["case_id"] != "HOLD-001":
        raise ValueError("Rotation is only applicable once to the consumed v1 holdout.")
    if any((BASE / f"H2-{n:03}").exists() for n in range(1, 16)):
        raise ValueError("Replacement inputs already exist; never regenerate after inference.")
    consumed = BASE / "consumed-manifest-holdout-v1.json"
    if consumed.exists():
        raise ValueError("Already consumed this holdout.")
    consumed.write_bytes(manifest_path.read_bytes())
    original_spec = author.spec

    def replacement_spec(cid, split, **kwargs):
        config = original_spec(cid, split, **kwargs)
        config.update(
            issuer="Atlas Wharf Services",
            recipient="Birchwood Components Ltd",
            entity="Birchwood Components Ltd",
            date="2026-10-22",
        )
        return config

    author.spec, author.text_lines = replacement_spec, panel_lines
    cases = []

    def add(n, rows, **options):
        case = author.author_case(f"H2-{n:03}", "holdout", rows, **options)
        case["family_id"] = "stage8-replacement-invoice-panels-v2"
        cases.append(case)

    f = author.fee
    add(1, [f("Export escort", Decimal("64.35")), f("Shuttle service", 21)])
    add(2, [f("Terminal survey", 46), f("THC", 34)], kind="pdf", multipage=True)
    add(3, [f("Rail appointment", 23), f("Truck waiting", 14)], attachment="docx", html=True)
    add(
        4,
        [f("Gate attendance", 31)],
        kind="jpg",
        operation="PENDING_REVIEW",
        risk=["IMAGE_TEXT_REQUIRES_VERIFICATION"],
    )
    add(
        5,
        [f("Container inspection", 22), f("Seal survey", 18)],
        kind="png",
        operation="PENDING_REVIEW",
        risk=["IMAGE_TEXT_REQUIRES_VERIFICATION"],
    )
    add(
        6,
        [f("Delivery check", 26), f("Cargo photography", 28)],
        kind="tiff",
        multipage=True,
        operation="PENDING_REVIEW",
        risk=["IMAGE_TEXT_REQUIRES_VERIFICATION"],
    )
    add(7, [f("Pallet rental", 30, qty=3, rate=10), f("Label printing", Decimal("4.50"))], kind="xls")
    add(8, [f("Transit escort", 48, "CHF"), f("Transit escort", 52, "CHF")], kind="xlsx")
    add(9, [f("Loading supervision", 35, "GBP"), f("Crating", 16, "GBP")], kind="doc")
    add(10, [f("Terminal visit", 47), f("Terminal visit", 47)], kind="docx", table=True)
    add(
        11,
        [f("Booking administration", 39)],
        old=[f("Booking administration", 39)],
        attachment="pdf",
        duplicate_sources=True,
        operation="NO_CHANGE",
    )
    add(
        12,
        [f("Correction attendance", 58)],
        old=[f("Correction attendance", 49)],
        operation="UPDATE",
        notes=["Please update amount from 49 to 58 for Correction attendance."],
        changes=[dict(path="/charge_details/amount", old=49, new=58)],
    )
    add(
        13,
        [f("Unassigned service", 33, "$", payment="COD")],
        old=[f("Unassigned service", 29), f("Unassigned service", 37)],
        operation="PENDING_REVIEW",
        risk=["MULTIPLE_MATCHES", "AMBIGUOUS_CURRENCY"],
    )
    credit = "Credit original reference EARLIER-772. Original payer: Birchwood Components Ltd; Original payee: Atlas Wharf Services"
    add(
        14,
        [f("Service allowance", Decimal("12.80"))],
        type="Credit Note",
        entity="Atlas Wharf Services",
        posting="AR",
        notes=[credit],
        signed=True,
        policy_quote=credit,
        policy={
            "adjustment": dict(
                kind="credit",
                original_reference="EARLIER-772",
                original_payer="Birchwood Components Ltd",
                original_payee="Atlas Wharf Services",
            )
        },
    )
    add(
        15,
        [f("Fault preservation", 44)],
        kind="pdf",
        faults={"ocr": "OCR_TIMEOUT_UNKNOWN_OUTCOME"},
        operation="PENDING_REVIEW",
        exit_code=3,
        risk=["OCR_TIMEOUT_UNKNOWN_OUTCOME"],
    )
    files = [
        dict(path=p.relative_to(BASE).as_posix(), sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        for case in cases
        for p in sorted((BASE / case["path"]).rglob("*"))
        if p.is_file()
    ]
    write_json(
        manifest_path,
        dict(
            schema_version="stage8-dataset-v1",
            split="holdout",
            annotation_review="assistant_authored_not_independently_signed",
            cases=cases,
            files=files,
            leakage_policy="Replacement description-first panel family stays wholly in holdout.",
            replaces_consumed_manifest=consumed.name,
        ),
    )
    write_json(
        BASE / "holdout-rotation.json",
        dict(
            reason="V1 raw fee descriptions included row identifiers; fixing after feedback consumes v1.",
            consumed_manifest=consumed.name,
            original_release="release.json",
            original_evaluation="outputs/stage8/holdout-v1/evaluation.json",
            replacement_family="stage8-replacement-invoice-panels-v2",
            replacement_cases=15,
            provenance="New raw layouts, parties and business cases authored before replacement inference; no labels enter model inputs.",
        ),
    )
    print("Preserved failed v1 evidence; authored and froze 15 new holdout panel cases. No model calls.")


if __name__ == "__main__":
    main()
