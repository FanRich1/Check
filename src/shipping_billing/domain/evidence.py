"""Recheck literal extraction facts and constrain context facts to the source family."""

import re
from decimal import Decimal

from shipping_billing.errors import BillingError
from shipping_billing.extraction.engine import contains_value

LABELS = {
    "tax": r"\b(?:TAX|VAT)\b|税额|税",
    "discount": r"\b(?:DISCOUNT|REBATE)\b|折扣|折让",
    "subtotal": r"\bSUB[ -]?TOTAL\b|小计",
    "total": r"(?<!SUB)\bTOTAL(?: AMOUNT| CHARGES TO BE PAID)?\b|合计|总计",
    "paid": r"\bPAID(?: AMOUNT)?\b|已付",
    "balance": r"\bBALANCE(?: DUE)?\b|未付|余额",
}


def labeled_amount(kind, value, refs, *, naked_total=False):
    number = r"(?<![\w.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![\w.])"
    for ref in refs:
        text = ref.quote
        for match in re.finditer(LABELS[kind], text, re.IGNORECASE):
            tail = re.split(r"[;|\n]", text[match.end() :], maxsplit=1)[0]
            found = re.search(number, tail)
            if (
                found
                and Decimal(found[0].replace(",", "")) == value
                and not re.match(r"\s*%", tail[found.end() :])
            ):
                return True
        if naked_total and re.fullmatch(r"\s*(?:[A-Z]{3}\s*[+-]?[\d,.]+|[+-]?[\d,.]+\s*[A-Z]{3})\s*", text):
            found = re.search(number, text)
            if found and Decimal(found[0].replace(",", "")) == value:
                return True
    return False


def labeled_currencies(kind, refs):
    from shipping_billing.domain.normalization import currency

    result = set()
    for ref in refs:
        for match in re.finditer(LABELS[kind], ref.quote, re.IGNORECASE):
            tail = re.split(r"[;|\n]", ref.quote[match.end() :], maxsplit=1)[0]
            for token in re.findall(r"\b[A-Z]{3}\b", tail.upper()):
                code, error = currency(token)
                if code and not error:
                    result.add(code)
    return result


class EvidenceIndex:
    def __init__(self, report):
        self.units = report.evidence_units
        if len({u.unit_id for u in self.units}) != len(self.units):
            raise BillingError("DUPLICATE_EVIDENCE_UNIT", "Evidence unit identities must be unique.")
        parents = {f.source_id: f.parent_source_id for f in report.files}
        if len(parents) != len(report.files):
            raise BillingError("DUPLICATE_SOURCE_ID", "Source identities must be unique.")
        self.roots = {}
        for source in parents:
            current, seen = source, set()
            while parents[current]:
                if current in seen or parents[current] not in parents:
                    raise BillingError("INVALID_SOURCE_TREE", "Source tree is cyclic or incomplete.")
                seen.add(current)
                current = parents[current]
            self.roots[source] = current
        if any(u.source_id not in parents for u in self.units):
            raise BillingError("UNKNOWN_EVIDENCE_SOURCE", "Evidence references an unregistered source.")
        from shipping_billing.extraction.business import same_document_sources
        self.same_documents = same_document_sources(self.units, self.roots)

    def verify(self, refs, value, source_id, *, native=False):
        if not refs:
            return False
        for ref in refs:
            if (self.roots.get(ref.source_id) != self.roots.get(source_id)
                    and ref.source_id not in self.same_documents.get(source_id, set())):
                return False
            matches = [
                u
                for u in self.units
                if u.source_id == ref.source_id and u.locator == ref.locator and ref.quote in u.text
            ]
            if not matches or (
                native
                and not any(
                    u.method
                    in (
                        "pdf_native_text",
                        "email_plain",
                        "email_html",
                        "email_header",
                        "docx_paragraph",
                        "word_table_row",
                        "xls_cells",
                        "xlsx_cells",
                        "authored_synthetic_fragment",
                    )
                    for u in matches
                )
            ):
                return False
        return value is None or contains_value(value, "\n".join(r.quote for r in refs))
