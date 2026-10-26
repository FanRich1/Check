"""Associate explicit business references and retain scoped, reviewable field proposals."""

import re
from collections import defaultdict
from itertools import combinations

from shipping_billing.extraction.contracts import BusinessLink, RelatedField
from shipping_billing.models import Evidence, Issue

REFERENCES = {
    "invoice": r"(?:INVOICE|DEBIT\s+NOTE|CREDIT\s+NOTE|D/C|账单|单据)(?:\s*(?:NO\.?|NUMBER|#|编号))?\s*[:：]\s*([A-Z0-9][A-Z0-9/_-]{3,79})",
    "mbl": r"(?:MBL|MASTER\s*(?:B/L|BL))(?:\s*(?:NO\.?|NUMBER|#))?\s*[:：]\s*([A-Z0-9][A-Z0-9/_-]{5,79})",
    "hbl": r"(?:HBL|HOUSE\s*(?:B/L|BL))(?:\s*(?:NO\.?|NUMBER|#))?\s*[:：]\s*([A-Z0-9][A-Z0-9/_-]{5,79})",
    "bl": r"(?:^|\n)\s*(?:B/L|BL)(?:\s*(?:NO\.?|NUMBER|#))?\s*[:：]\s*([A-Z0-9][A-Z0-9/_-]{5,79})",
    "booking": r"(?:BOOKING|订舱)(?:\s*(?:NO\.?|NUMBER|#|编号))?\s*[:：]\s*([A-Z0-9][A-Z0-9/_-]{5,79})",
    "container": r"(?:CONTAINER|CNTR|箱号)(?:\s*(?:NO\.?|NUMBER|#))?\s*[:：]\s*([A-Z]{4}\d{7})\b",
}
FIELDS = {
    "/settlement/payer": (r"(?:PAYER|PAYABLE BY|PAID BY|付款方|付款人)", False),
    "/settlement/payee": (r"(?:PAYEE|PAYABLE TO|PAID TO|收款方|收款人)", False),
    "/transaction_party/issuer": (r"(?:ISSUER|SUPPLIER|SELLER|签发方)", False),
    "/transaction_party/recipient": (r"(?:BILL\s*TO|BILLED\s*TO|接收方)", False),
    "/charge_details/currency": (r"(?:CURRENCY|币种)", False),
}
HINTS = {
    "/settlement/payer": r"(?:CONSIGNEE|收货人)",
    "/settlement/payee": r"(?:REMIT\s*TO|BENEFICIARY|汇款对象|受益人)",
}
NATIVE_METHODS = {"pdf_native_text", "email_plain", "email_html", "email_header", "xlsx_cells", "xls_cells",
                  "docx_paragraph", "word_table_row", "authored_synthetic_fragment"}


def proof(unit):
    return Evidence(source_id=unit.source_id, locator=unit.locator, quote=unit.text)


def references(units):
    """Only literal labeled identifiers count; filenames and naked numbers do not."""
    result = defaultdict(lambda: defaultdict(list))
    for unit in units:
        for kind, pattern in REFERENCES.items():
            for match in re.finditer(pattern, unit.text, re.I):
                value = match[1].upper()
                if any(c.isdigit() for c in value):
                    result[unit.source_id][(kind, value)].append(proof(unit))
    return result


def same_document_sources(units, roots):
    """Rebuilt from original units by the domain layer, never trusted from model output."""
    refs = references(u for u in units if u.method in NATIVE_METHODS)
    groups = defaultdict(set)
    for sid, values in refs.items():
        invoices = {value for kind, value in values if kind == "invoice"}
        if len(invoices) == 1:
            groups[next(iter(invoices))].add(sid)
    result = defaultdict(set)
    for group in groups.values():
        for a in group:
            result[a].add(a)
            for b in group - {a}:
                transport = {key for key in refs[a] if key[0] != "invoice"} & set(refs[b])
                if roots.get(a) == roots.get(b) or transport:
                    result[a].add(b)
    return result


def field_facts(units):
    """Billing labels are distinct from shipping and bank-role suggestions."""
    facts = defaultdict(list)
    for unit in units:
        for path, (label, inferred) in FIELDS.items():
            for match in re.finditer(r"(?:^|[\n\t|;])\s*" + label + r"\s*[:：]\s*([^\n\t|;]+)", unit.text, re.I):
                value = match[1].strip()
                if path.endswith("/currency"):
                    if not re.fullmatch(r"[A-Z]{3}", value):
                        continue
                    from shipping_billing.domain.normalization import currency
                    if currency(value)[1]:
                        continue
                elif not value or len(value) > 300:
                    continue
                facts[path].append((value, proof(unit), inferred or unit.method not in NATIVE_METHODS))
        for path, label in HINTS.items():
            for match in re.finditer(r"(?:^|[\n\t|;])\s*" + label + r"\s*[:：]\s*([^\n\t|;]+)", unit.text, re.I):
                value = match[1].strip()
                if value and len(value) <= 300:
                    facts[path].append((value, proof(unit), True))
        allocation = re.fullmatch(
            r"\s*([A-Za-z][A-Za-z0-9 ._-]{0,79}?)\s+pay\s+([A-Za-z][A-Za-z0-9 ._-]{0,79}?)\s*[:：].+",
            unit.text, re.I)
        if allocation:
            for path, value in zip(("/settlement/payer", "/settlement/payee"), allocation.groups(), strict=True):
                facts[path].append((value.strip(), proof(unit), True))
    return facts


def current_value(candidate, path):
    group, field = path.strip("/").split("/")
    if group == "settlement":
        from shipping_billing.domain.finance import document_kind
        if document_kind(candidate.document_info.document_type) == "normal":
            return getattr(candidate.transaction_party, "recipient" if field == "payer" else "issuer")
        return None
    return getattr(getattr(candidate, group), field)


def value_key(path, value):
    from shipping_billing.domain.normalization import party_key
    return value.upper() if path.endswith("/currency") else party_key(value)


def enrich_business_context(report, roots):
    units = defaultdict(list)
    for unit in report.evidence_units:
        # OCR structured fields may repeat or invent business roles. Only source text is used.
        if unit.method != "specified_ocr_structured":
            units[unit.source_id].append(unit)
    refs = references(report.evidence_units)
    exact = same_document_sources(report.evidence_units, roots)
    by_id = {file.source_id: file for file in report.files}
    links = []
    for file in report.files:
        if file.parent_source_id:
            links.append(BusinessLink(source_id=file.parent_source_id,
                                      related_source_id=file.source_id, kind="email_attachment"))
    inverted = defaultdict(list)
    for sid, values in refs.items():
        for key in values:
            inverted[key].append(sid)
    for (kind, value), ids in sorted(inverted.items()):
        for a, b in combinations(sorted(ids), 2):
            evidence = refs[a][(kind, value)] + refs[b][(kind, value)]
            links.append(BusinessLink(source_id=a, related_source_id=b,
                                      kind=("same_document" if b in exact.get(a, set()) else "same_reference")
                                      if kind == "invoice" else "same_shipment",
                                      reference_kind=kind, reference_value=value, evidence=evidence))
    # A generic B/L label can match an explicit master/house label by the exact same number.
    for (kind, value), ids in sorted(inverted.items()):
        if kind != "bl":
            continue
        for explicit in ("mbl", "hbl"):
            for a in ids:
                for b in inverted.get((explicit, value), []):
                    if a != b:
                        links.append(BusinessLink(source_id=a, related_source_id=b, kind="same_shipment",
                                                  reference_kind=explicit, reference_value=value,
                                                  evidence=refs[a][(kind, value)] + refs[b][(explicit, value)]))
    report.business_links = links
    facts = {sid: field_facts(source_units) for sid, source_units in units.items()}
    peers = defaultdict(set)
    for link in links:
        if link.kind != "email_attachment":
            peers[link.source_id].add(link.related_source_id)
            peers[link.related_source_id].add(link.source_id)
    for file in report.files:
        own_invoices = {v for k, v in refs[file.source_id] if k == "invoice"}
        for candidate in file.candidates:
            proposals = defaultdict(list)
            # Own labeled roles also help notices/emails whose parties lack invoice semantics.
            for sid in sorted({file.source_id, *peers[file.source_id]}):
                if by_id[sid].status == "error":
                    continue
                for path, values in facts.get(sid, {}).items():
                    for value, evidence, inferred in values:
                        if inferred and current_value(candidate, path) is not None:
                            continue
                        auto = (not inferred and sid in exact.get(file.source_id, set())
                                and len(own_invoices) == 1
                                and candidate.document_info.reference_no is not None
                                and candidate.document_info.reference_no.upper() in own_invoices)
                        proposals[path].append((value, evidence, auto, inferred))
            for path, values in proposals.items():
                unique = {value_key(path, v) for v, *_ in values}
                current = current_value(candidate, path)
                conflict = len(unique) > 1 or (current is not None and value_key(path, current) not in unique)
                # Only missing document-header fields may be completed automatically.
                auto = not conflict and current is None and all(v[2] for v in values)
                if path == "/charge_details/currency" and candidate.charge_details.currency_raw:
                    auto = False  # Preserve ambiguous original symbols for explicit review.
                shown = {}
                for value, evidence, supported, inferred in values:
                    status = "conflict" if conflict else "applied" if auto else "suggested"
                    reason = ("关联材料的字段存在不同取值，需人工核对。" if conflict else
                              "同一账单编号下的明确字段，已补充缺失值。" if auto else
                              "原文提供了角色线索，需确认公司全称及对本笔费用的适用性。" if inferred else
                              "相关材料提供了明确字段，但尚需确认是否适用于本笔费用。")
                    # Matching own values are already represented in field evidence.
                    if current is not None and not conflict and value_key(path, current) == value_key(path, value):
                        continue
                    key = (value_key(path, value), evidence.source_id)
                    if key in shown:
                        if evidence not in shown[key].evidence:
                            shown[key].evidence.append(evidence)
                        continue
                    field = RelatedField(path=path, value=value, source_id=evidence.source_id,
                                         status=status, reason=reason, evidence=[evidence])
                    shown[key] = field
                    candidate.related_fields.append(field)
                if auto and not path.startswith("/settlement/"):
                    group, name = path.strip("/").split("/")
                    value = values[0][0]
                    setattr(getattr(candidate, group), name, value)
                    evidence = [v[1] for v in values]
                    candidate.field_evidence[path] = evidence
                    if path == "/charge_details/currency":
                        candidate.charge_details.currency_raw = value
                        candidate.field_evidence["/charge_details/currency_raw"] = evidence
                        candidate.issues = [i for i in candidate.issues if i.code != "CURRENCY_UNRESOLVED"]
                    candidate.evidence.extend(evidence)
                if conflict:
                    candidate.issues.append(Issue(code="RELATED_BUSINESS_FIELD_CONFLICT", severity="warning",
                                                  blocks_automation=True, field=path,
                                                  message="Related business fields disagree; do not overwrite."))
            # An inferred role pair must always be reviewed, even when other core fields are present.
            if any(f.status == "suggested" and f.path.startswith("/settlement/") for f in candidate.related_fields):
                candidate.issues.append(Issue(code="RELATED_PARTIES_REQUIRE_CHECK", severity="warning",
                                              blocks_automation=True,
                                              message="Related payer/payee candidates need confirmation."))
        if file.status != "error" and file.candidates:
            file.status = "pending_review" if any(i.blocks_automation for i in file.issues) or any(
                i.blocks_automation for c in file.candidates for i in c.issues) else "extracted"
