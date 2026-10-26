"""Exact alias maps and scoped payment terms. No fuzzy entity matching."""

import re
from importlib.resources import files

import simplejson

from shipping_billing.errors import BillingError


def key(value):
    return " ".join(value.split()).casefold()


def party_key(value):
    """Ignore the optional final full stop on an explicit company suffix only."""
    return re.sub(r"\b(inc|ltd|llc|corp|co)\.$", r"\1", key(value))


def alias_map(aliases, *, normalize_key=key):
    result = {}
    for alias, canonical in aliases.items():
        token = normalize_key(alias)
        if token in result and normalize_key(result[token]) != normalize_key(canonical):
            raise BillingError("CONFLICTING_ALIAS", "Aliases with equal normalized spelling conflict.")
        result[token] = canonical
    for token in result:
        seen = set()
        current = token
        while current in result and normalize_key(result[current]) != current:
            if current in seen:
                raise BillingError("CYCLIC_ALIAS", "Alias configuration contains a cycle.")
            seen.add(current)
            current = normalize_key(result[current])
    return result


def resolve(value, aliases, *, normalize_key=key):
    if value is None:
        return None
    current = value
    while normalize_key(current) in aliases:
        target = aliases[normalize_key(current)]
        if normalize_key(target) == normalize_key(current):
            return target
        current = target
    return " ".join(current.split())


def party_alias_map(aliases):
    return alias_map(aliases, normalize_key=party_key)


def resolve_party(value, aliases):
    return resolve(value, aliases, normalize_key=party_key)


CHARGES = {
    "Handling Fee": "Handling Charge",
    "H/D Fee": "Handling Charge",
    "H/C": "Handling Charge",
    "Handling Charge": "Handling Charge",
    "Doc Fee": "Documentation Fee",
    "Documentation Fee": "Documentation Fee",
    "THC": "Terminal Handling Charge",
    "Terminal Handling Charge": "Terminal Handling Charge",
    "Trucking Fee": "Trucking Charge",
    "Trucking Charge": "Trucking Charge",
    "Customs Fee": "Customs Clearance Fee",
    "Customs Clearance Fee": "Customs Clearance Fee",
    "Storage Fee": "Storage Charge",
    "Storage Charge": "Storage Charge",
    # A reviewed carrier-specific code+description pair, not a rule stripping numeric prefixes.
    "OCA17 On Carriage Additional - US Em": "On Carriage Additional - US Em",
}

SNAPSHOT = simplejson.loads(
    files("shipping_billing.domain").joinpath("data/iso4217.json").read_text(encoding="utf-8")
)
CURRENCIES = SNAPSHOT["minor_units"]
CURRENCY_ALIASES = {
    "RMB": "CNY",
    "人民币": "CNY",
    "US$": "USD",
    "US DOLLAR": "USD",
    "US DOLLARS": "USD",
    "美元": "USD",
    "EURO": "EUR",
    "EUROS": "EUR",
    "€": "EUR",
    "英镑": "GBP",
    "港币": "HKD",
    "HK$": "HKD",
    "CN¥": "CNY",
    "JP¥": "JPY",
    "JPY¥": "JPY",
    "CA$": "CAD",
    "CAD$": "CAD",
    "AU$": "AUD",
    "AUD$": "AUD",
    "NZ$": "NZD",
}


def currency(raw):
    if raw is None or not raw.strip():
        return None, "MISSING_CURRENCY"
    token = " ".join(raw.split()).upper()
    if token in ("$", "¥", "￥", "£"):
        return None, "AMBIGUOUS_CURRENCY"
    code = CURRENCY_ALIASES.get(token, token)
    if code not in CURRENCIES:
        if code in SNAPSHOT.get("historical_codes", []):
            return None, "HISTORICAL_CURRENCY_REQUIRES_REVIEW"
        return None, "INVALID_CURRENCY"
    if CURRENCIES[code] is None:
        return code, "CURRENCY_WITHOUT_MINOR_UNIT"
    return code, None


def payment(raw, *, scope, is_freight):
    if not raw:
        return None, None, None
    token = " ".join(raw.upper().split())
    if token in ("COD", "C.O.D.", "CASH ON DELIVERY"):
        return None, "COD", "PAYMENT_CONDITION_ONLY"
    if token in ("PAY IMMEDIATELY", "DUE ON RECEIPT") or re.fullmatch(r"NET\s+\d+", token):
        return None, token, "PAYMENT_CONDITION_ONLY"
    pp = token in ("PP", "P/P", "PPD", "PREPAID", "FREIGHT PREPAID")
    cc = token in ("CC", "C/C", "COLLECT", "FREIGHT COLLECT")
    if re.search(r"\b(PREPAID|PP)\b", token) and re.search(r"\b(COLLECT|CC)\b", token):
        return None, None, "PAYMENT_TERM_CONFLICT"
    if not pp and not cc:
        return None, None, "PAYMENT_TERM_UNMAPPED"
    if ("FREIGHT" in token or scope == "freight") and not is_freight:
        return None, None, "PAYMENT_SCOPE_UNCLEAR"
    if scope == "unknown" and "FREIGHT" not in token:
        return None, None, "PAYMENT_SCOPE_UNCLEAR"
    return "PP" if pp else "CC", None, None
