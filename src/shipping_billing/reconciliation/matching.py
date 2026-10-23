"""Matching keys never use missing-value equality or new amounts to select a same-name row."""

from shipping_billing.domain.finance import document_kind
from shipping_billing.domain.normalization import currency, key, resolve
from shipping_billing.extraction.engine import digest
from shipping_billing.models import same_json_value as equal


def read(data, path):
    value = data
    for part in path.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def write(data, path, value):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.strip("/").split("/")]
    current = data
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        if not isinstance(current[part], dict):
            raise ValueError("cannot descend through a scalar field")
        current = current[part]
    current[parts[-1]] = value


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def fee(data, charge_aliases):
    value = read(data, "/charge_details/charge_item")
    return key(resolve(value, charge_aliases)) if value else None


def party(value, aliases):
    return key(resolve(value, aliases)) if value else None


def separate_adjustment(new, old):
    a = document_kind(read(new, "/document_info/document_type"))
    b = document_kind(read(old, "/document_info/document_type"))
    return a in ("normal", "credit", "reversal") and b in ("normal", "credit", "reversal") and a != b


def hard_conflicts(new, old, party_aliases):
    conflicts = []
    for path in (
        "/document_info/reference_no",
        "/shipping_info/mbl_no",
        "/shipping_info/hbl_no",
        "/shipping_info/booking_no",
    ):
        a, b = text(read(new, path)), text(read(old, path))
        if a and b and a != b:
            conflicts.append(path)
    for path in (
        "/transaction_party/issuer",
        "/transaction_party/recipient",
        "/financial_attributes/accounting_entity",
    ):
        a, b = party(read(new, path), party_aliases), party(read(old, path), party_aliases)
        if a and b and a != b:
            conflicts.append(path)
    for path in ("/charge_details/currency", "/financial_attributes/posting_type"):
        a, b = read(new, path), read(old, path)
        if a is not None and b is not None and a != b:
            conflicts.append(path)
    a, b = (
        set(read(new, "/shipping_info/container_nos") or []),
        set(read(old, "/shipping_info/container_nos") or []),
    )
    if a and b and not a.intersection(b):
        conflicts.append("/shipping_info/container_nos")
    return conflicts


def level(new, old, charges, parties):
    same_fee = fee(new, charges) is not None and fee(new, charges) == fee(old, charges)
    ref = text(read(new, "/document_info/reference_no"))
    if same_fee and ref and ref == text(read(old, "/document_info/reference_no")):
        return 1
    code = read(new, "/charge_details/currency")
    same_currency = code is not None and code == read(old, "/charge_details/currency")
    if same_fee and same_currency:
        for n, field in ((2, "mbl_no"), (2, "hbl_no"), (3, "booking_no")):
            value = text(read(new, "/shipping_info/" + field))
            if value and value == text(read(old, "/shipping_info/" + field)):
                return n
    containers = set(read(new, "/shipping_info/container_nos") or []) & set(
        read(old, "/shipping_info/container_nos") or []
    )
    identities = all(
        party(read(new, "/transaction_party/" + p), parties) is not None
        and party(read(new, "/transaction_party/" + p), parties)
        == party(read(old, "/transaction_party/" + p), parties)
        for p in ("issuer", "recipient")
    )
    date = read(new, "/document_info/document_date")
    amount = read(new, "/charge_details/amount")
    if (
        containers
        and identities
        and date
        and date == read(old, "/document_info/document_date")
        and amount is not None
        and amount == read(old, "/charge_details/amount")
    ):
        return 4
    return None


def occurrence(line, extraction, source_hashes):
    refs = line.original.field_evidence.get("/charge_details/charge_item_raw", [])
    if not refs:
        refs = line.original.field_evidence.get("/charge_details/charge_item", [])
    locations = []
    for ref in refs:
        if ref.source_id != line.source_id:
            continue
        loc = ref.locator.model_dump(mode="python", exclude_none=True)
        for field, value in loc.items():
            if isinstance(value, str):
                loc[field] = value.replace(line.source_id + ":", "<source>:")
        locations.append(loc)
    units = [u for u in extraction.evidence_units if u.source_id == line.source_id]
    document = source_hashes.get(line.source_id) or digest([u.text for u in units])
    # A concrete row locator is necessary to tell two same-name/same-amount rows apart.
    detailed = locations and all(
        any(loc.get(k) for k in ("section", "region", "cells", "block")) for loc in locations
    )
    if not detailed:
        return None, document
    return digest(["physical-v1", document, locations, line.financial_attributes.accounting_entity]), document


def same_value(path, a, b, charges, parties):
    if path == "/charge_details/charge_item":
        return (key(resolve(a, charges)) if a else a) == (key(resolve(b, charges)) if b else b)
    if path.startswith("/transaction_party/") or path == "/financial_attributes/accounting_entity":
        return party(a, parties) == party(b, parties)
    if path == "/document_info/document_type":
        return key(a) == key(b) if a and b else a == b
    if path == "/shipping_info/container_nos":
        return set(a or []) == set(b or [])
    return equal(a, b)


def valid_money(data):
    from shipping_billing.domain.money import quantized

    amount, raw = read(data, "/charge_details/amount"), read(data, "/charge_details/currency")
    code, error = currency(raw)
    if error or code != raw or amount is None:
        return "INVALID_MERGED_CORE"
    from shipping_billing.domain.normalization import CURRENCIES

    if amount != quantized(amount, CURRENCIES[code]):
        return "MERGED_AMOUNT_PRECISION"
    return None
