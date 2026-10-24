"""Bounded LangChain extraction with verified citations and exact-input replay."""

import hashlib
import re
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from shipping_billing.errors import BillingError
from shipping_billing.extraction.contracts import (
    ExtractedCandidate,
    ExtractionDraft,
    ExtractionFile,
    ExtractionReport,
)
from shipping_billing.extraction.prompt import INSTRUCTIONS, PROMPT_VERSION
from shipping_billing.extraction.units import build_units
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.models import ChargeDetails, DocumentInfo, Evidence, Issue, TransactionParty


def digest(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def issue(code, field=None):
    return Issue(
        code=code,
        severity="warning",
        blocks_automation=True,
        field=field,
        message="Extraction requires review: " + code,
    )


@lru_cache(maxsize=1)
def currency_tokens():
    from shipping_billing.domain.normalization import CURRENCIES

    return "|".join(sorted(CURRENCIES))


def contains_value(value, text):
    if isinstance(value, Decimal | int | float):
        codes = currency_tokens()
        text = re.sub(r"(?<![A-Za-z])(" + codes + r")(?=[+-]?\d)", r"\1 ", text)
        text = re.sub(r"(?<=\d)(" + codes + r")(?![A-Za-z])", r" \1", text)
        numbers = re.findall(r"(?<![A-Za-z0-9_.-])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z0-9_]|\.\d)", text)
        return Decimal(str(value)) in [Decimal(n.replace(",", "")) for n in numbers]
    if isinstance(value, list):
        return all(contains_value(v, text) for v in value)
    return str(value) in text


def checked_value(value, citations, units, allowed, field, issues, evidence):
    if value is None:
        return None
    refs = []
    for citation in citations:
        unit = units.get(citation.unit_id)
        if unit is None or unit.source_id not in allowed or citation.quote not in unit.text:
            continue
        if isinstance(value, Decimal | int | float) and not contains_value(value, unit.text):
            continue
        refs.append((unit, citation.quote))
    if not refs or not contains_value(value, "\n".join(q for _, q in refs)):
        issues.append(issue("UNSUPPORTED_FIELD_EVIDENCE", field))
        return None
    evidence[field] = [Evidence(source_id=u.source_id, locator=u.locator, quote=q) for u, q in refs]
    return value


def materialize(draft, target, units, allowed):
    candidates, problems = [], []
    seen = set()
    for doc in draft.documents:
        header_issues, header_evidence = [], {}
        headers = {}
        for key in ("document_type", "reference_no", "document_date_raw", "issuer", "recipient"):
            headers[key] = checked_value(
                getattr(doc, key),
                doc.evidence.get(key, []),
                units,
                allowed,
                key,
                header_issues,
                header_evidence,
            )
        shipping = {}
        for key, value in doc.shipping_info.items():
            field = "shipping_info/" + key
            result = checked_value(
                value, doc.evidence.get(field, []), units, allowed, field, header_issues, header_evidence
            )
            if result is not None:
                shipping[key] = result
        for fee in doc.fees:
            problems_for_fee, field_evidence = list(header_issues), dict(header_evidence)
            fields = {}
            for key in Fee_KEYS:
                fields[key] = checked_value(
                    getattr(fee, key),
                    fee.evidence.get(key, []),
                    units,
                    allowed,
                    key,
                    problems_for_fee,
                    field_evidence,
                )
            # Physical row identity is supplied by verified unit references, never fee name alone.
            name_units = {
                c.unit_id
                for c in fee.evidence.get("charge_item_raw", [])
                if c.unit_id in units
                and units[c.unit_id].source_id == target.source_id
                and c.quote in units[c.unit_id].text
                and fee.charge_item_raw
                and fee.charge_item_raw in c.quote
            }
            amount_units = {
                c.unit_id
                for c in fee.evidence.get("amount", [])
                if c.unit_id in units
                and c.quote in units[c.unit_id].text
                and fee.amount is not None
                and contains_value(fee.amount, c.quote)
            }
            if not name_units and any(
                c.unit_id in units and units[c.unit_id].source_id != target.source_id
                for c in fee.evidence.get("charge_item_raw", [])
            ):
                problems.append(issue("RELATED_SOURCE_FEE_NOT_OWNED"))
                continue
            row_units = name_units & amount_units if fee.amount is not None else name_units
            if not row_units and name_units and amount_units:
                # Recover one actual contiguous edit paragraph, not arbitrary concatenated lines.
                paragraphs = []
                for unit in units.values():
                    span = unit.metadata.get("original_contiguous_lines")
                    if (
                        unit.source_id != target.source_id
                        or not span
                        or fee.charge_item_raw not in unit.text
                        or not contains_value(fee.amount, unit.text)
                    ):
                        continue
                    prefix = (unit.locator.section or "").split(":edit-paragraph-")[0] + ":line-"

                    def inside_span(uid):
                        if uid == unit.unit_id:
                            return True
                        section = units[uid].locator.section or ""
                        return (
                            section.startswith(prefix)
                            and section[len(prefix) :].isdigit()
                            and span[0] <= int(section[len(prefix) :]) <= span[1]
                        )

                    if any(inside_span(uid) for uid in name_units) and any(
                        inside_span(uid) for uid in amount_units
                    ):
                        paragraphs.append(unit)
                if len(paragraphs) == 1:
                    unit = paragraphs[0]
                    proof = Evidence(source_id=unit.source_id, locator=unit.locator, quote=unit.text)
                    field_evidence["charge_item_raw"] = [proof]
                    field_evidence["amount"] = [proof]
                    row_units = {unit.unit_id}
            if not row_units:
                problems_for_fee.append(issue("FEE_ROW_EVIDENCE_MISMATCH"))
            identity = sorted(row_units or name_units)
            line_key = digest([target.source_id, identity, fields["charge_item_raw"]])
            if identity and line_key in seen:
                problems.append(issue("DUPLICATE_PHYSICAL_FEE_ROW"))
                continue
            seen.add(line_key)
            currency_raw = fields["currency_raw"]
            currency = currency_raw if currency_raw and re.fullmatch(r"[A-Z]{3}", currency_raw) else None
            if currency is None:
                problems_for_fee.append(issue("CURRENCY_UNRESOLVED", "currency_raw"))
            if fields["amount"] is None or fields["charge_item_raw"] is None:
                problems_for_fee.append(issue("FEE_CORE_FACT_MISSING"))
            path_map = {
                k: "/document_info/" + k for k in ("document_type", "reference_no", "document_date_raw")
            }
            path_map.update({k: "/transaction_party/" + k for k in ("issuer", "recipient")})
            path_map.update({k: "/charge_details/" + k for k in Fee_KEYS})
            path_map.update({"shipping_info/" + k: "/shipping_info/" + k for k in shipping})
            mapped = {path_map[k]: v for k, v in field_evidence.items()}
            for original, alias in (("charge_item_raw", "charge_item"), ("currency_raw", "currency")):
                if original == "currency_raw" and currency is None:
                    continue
                src = "/charge_details/" + original
                if src in mapped:
                    mapped["/charge_details/" + alias] = mapped[src]
            candidates.append(
                ExtractedCandidate(
                    source_id=target.source_id,
                    source_line_id=target.source_id + ":line-" + line_key[:20],
                    document_info=DocumentInfo(
                        document_type=headers["document_type"],
                        reference_no=headers["reference_no"],
                        document_date=None,
                        document_date_raw=headers["document_date_raw"],
                    ),
                    transaction_party=TransactionParty(
                        issuer=headers["issuer"], recipient=headers["recipient"]
                    ),
                    charge_details=ChargeDetails(
                        charge_item=fields["charge_item_raw"],
                        amount=fields["amount"],
                        currency=currency,
                        payment_term=None,
                        **{k: fields[k] for k in Fee_KEYS if k != "amount"},
                    ),
                    shipping_info=shipping,
                    field_evidence=mapped,
                    evidence=[e for refs in mapped.values() for e in refs],
                    issues=list(target.issues) + problems_for_fee,
                )
            )
    return candidates, problems


Fee_KEYS = ("charge_item_raw", "amount", "currency_raw", "quantity", "unit_price", "payment_term_raw")


def verify_native_evidence(target, file, units):
    """Resolve generic vendor warnings only after successful OCR and native field checks.

    Actual conflicts, missing roles and delivery failures remain blocking. Raw parse
    evidence is retained unchanged; this decision belongs to the extraction result.
    """
    generic = {"OCR_STRUCTURED_REQUIRES_CHECK", "OCR_NO_ORIGINAL_TEXT"}
    pages = target.metadata.get("pages", 0)
    native = [u for u in units.values() if u.source_id == target.source_id and u.method == "pdf_native_text"]
    completed = {p["page"] for p in target.metadata.get("ocr_page_status", []) if p["status"] == "completed"}
    if (target.detected_format != "pdf" or not pages or not file.candidates
            or completed != set(range(1, pages + 1))
            or {u.locator.page for u in native} != completed):
        return
    all_issues = file.issues + [i for c in file.candidates for i in c.issues]
    if any(i.blocks_automation and i.code not in generic for i in all_issues):
        return
    for candidate in file.candidates:
        for field in ("charge_item", "amount", "currency"):
            if not candidate.field_evidence.get("/charge_details/" + field):
                return
        for refs in candidate.field_evidence.values():
            if not refs or any(not any(
                ref.source_id == u.source_id and ref.locator == u.locator and ref.quote in u.text
                for u in native
            ) for ref in refs):
                return
    if not any(i.code in generic and i.blocks_automation for i in all_issues):
        return
    def verified(issues):
        return [i.model_copy(update={"blocks_automation": False}) if i.code in generic else i for i in issues]
    file.issues = verified(file.issues)
    proof = Issue(code="NATIVE_TEXT_VERIFIED", severity="info", blocks_automation=False,
                  message="Extracted fields verified against native PDF text; OCR completed on every page.")
    file.issues.append(proof)
    for candidate in file.candidates:
        candidate.issues = verified(candidate.issues) + [proof]


class ExtractionEngine:
    def __init__(
        self,
        client=None,
        *,
        storage=Path("data/local/extraction"),
        mode="live",
        max_input_chars=120000,
        repair_attempts=1,
    ):
        if mode not in ("live", "replay", "test_injected") or repair_attempts not in (0, 1):
            raise ValueError("invalid extraction mode or repair bound")
        self.client, self.storage, self.mode = client, Path(storage), mode
        self.max_input_chars, self.repair_attempts = max_input_chars, repair_attempts

    def extract(self, parsed):
        by_id = {s.source_id: s for s in parsed.sources}
        if len(by_id) != len(parsed.sources):
            raise BillingError("DUPLICATE_SOURCE_ID", "Parsing source identities must be unique.")

        def root(source):
            visited = set()
            while source.parent_source_id:
                if source.source_id in visited or source.parent_source_id not in by_id:
                    raise BillingError(
                        "INVALID_SOURCE_TREE", "Source tree is cyclic or has a missing parent."
                    )
                visited.add(source.source_id)
                source = by_id[source.parent_source_id]
            return source.source_id

        roots = {s.source_id: root(s) for s in parsed.sources}
        unit_list = build_units(parsed)
        units = {u.unit_id: u for u in unit_list}
        report = ExtractionReport(
            mode=self.mode,
            prompt_version=PROMPT_VERSION,
            input_sha256=digest(parsed.model_dump(mode="python")),
            evidence_units=unit_list,
        )
        for target in parsed.sources:
            file = ExtractionFile(
                source_id=target.source_id,
                parent_source_id=target.parent_source_id,
                status="error",
                issues=list(target.issues),
            )
            report.files.append(file)
            if target.status == "error":
                file.issues.append(issue("SOURCE_PARSE_ERROR"))
                continue
            allowed = {key for key in by_id if roots[key] == roots[target.source_id]}
            # Extract fees from their own source first. Related shipping context is joined below
            # by explicit shared identifiers; attachments cannot suppress or create body fees.
            selected = [u for u in unit_list if u.source_id == target.source_id]
            payload = {
                "target": target.source_id,
                "related_sources": sorted(allowed - {target.source_id}),
                "sources": [
                    {"source_id": s.source_id, "sha256": s.sha256, "parent_source_id": s.parent_source_id}
                    for s in parsed.sources
                    if s.source_id in allowed
                ],
                "target_units": [
                    u.model_dump(mode="python", exclude_none=True)
                    for u in selected
                    if u.source_id == target.source_id
                ],
            }
            text = dumps(payload)
            fingerprint = digest([PROMPT_VERSION, INSTRUCTIONS, ExtractionDraft.model_json_schema(), payload])
            archive_path = self.storage / (fingerprint + ".json")
            if not any(u.source_id == target.source_id for u in selected):
                file.issues.append(issue("NO_SOURCE_TEXT"))
                continue
            if len(text) > self.max_input_chars:
                file.issues.append(issue("MODEL_INPUT_LIMIT_EXCEEDED"))
                continue
            try:
                if self.mode == "replay":
                    archive = load_json(archive_path)
                    if not isinstance(archive, dict):
                        raise BillingError("MODEL_REPLAY_INVALID", "Replay archive must be an object.")
                    if (
                        archive.get("fingerprint") != fingerprint
                        or archive.get("prompt_version") != PROMPT_VERSION
                    ):
                        raise BillingError(
                            "MODEL_REPLAY_MISMATCH", "Replay does not match the exact extraction input."
                        )
                    file.attempts = archive["attempts"]
                    if archive.get("error"):
                        raise BillingError(archive["error"], "Replayed extraction failure.")
                    draft = ExtractionDraft.model_validate(archive["draft"])
                else:
                    draft = None
                    archive = {
                        "fingerprint": fingerprint,
                        "prompt_version": PROMPT_VERSION,
                        "attempts": file.attempts,
                        "mode": self.mode,
                    }
                    for attempt in range(self.repair_attempts + 1):
                        record = {"attempt": attempt + 1, "repair": attempt > 0}
                        file.attempts.append(record)
                        report.model_invocations += 1
                        try:
                            draft, meta = self.client.extract(
                                text,
                                ExtractionDraft,
                                instructions=INSTRUCTIONS
                                + (
                                    "\nPrevious output violated schema. Re-extract valid JSON."
                                    if attempt
                                    else ""
                                ),
                                prompt_version=PROMPT_VERSION,
                                response_sink=lambda content: record.update(raw_content=content),
                            )
                            record.update(metadata=meta, status="success")
                            archive["draft"] = draft.model_dump(mode="python")
                            write_json(archive_path, archive)
                            break
                        except BillingError as exc:
                            record.update(status="error", error=exc.code)
                            archive["error"] = exc.code
                            write_json(archive_path, archive)
                            if attempt >= self.repair_attempts or exc.code != "MODEL_INVALID_STRUCTURE":
                                raise
                            archive.pop("error", None)
                file.candidates, problems = materialize(draft, target, units, allowed)
                file.issues.extend(problems)
                verify_native_evidence(target, file, units)
                for observation in draft.observations:
                    if all(
                        c.unit_id in units
                        and c.quote in units[c.unit_id].text
                        and units[c.unit_id].source_id in allowed
                        for c in observation.citations
                    ):
                        file.observations.append(observation)
                    else:
                        file.issues.append(issue("UNSUPPORTED_OBSERVATION_EVIDENCE"))
                blockers = any(i.blocks_automation for i in file.issues) or any(
                    i.blocks_automation for c in file.candidates for i in c.issues
                )
                file.status = (
                    "pending_review" if blockers else "extracted" if file.candidates else "no_charge"
                )
            except (BillingError, ValidationError, KeyError) as exc:
                file.issues.append(
                    issue(exc.code if isinstance(exc, BillingError) else "MODEL_REPLAY_INVALID")
                )
                file.status = "error"
        supplement_email_allocations(report)
        enrich_related_shipping(report, roots)
        return report


def supplement_email_allocations(report):
    """Retain explicit one-line email cost allocations missed by the model.

    A narrowly anchored grammar, source body only, always requiring human review.
    It does not interpret conditional terms, execute instructions or infer parties/currency.
    """
    pattern = re.compile(
        r"^\s*[A-Za-z][A-Za-z0-9 _.-]{0,39}\s+pay\s+[A-Za-z][A-Za-z0-9 _.-]{0,39}"
        r"\s*[:：]\s*(?P<name>[^$€£\n:：]{1,80}?)\s*"
        r"(?P<currency>[$€£]|[A-Z]{3})\s*(?P<amount>[+-]?\d+(?:,\d{3})*(?:\.\d+)?)[.。]?\s*$",
        re.IGNORECASE,
    )
    for file in report.files:
        if file.status == "error":
            continue
        for unit in report.evidence_units:
            if unit.source_id != file.source_id or unit.method not in ("email_plain", "email_html"):
                continue
            if re.search(
                r"\b(if|unless|may|would|ignore|system|output|instruction)\b", unit.text, re.IGNORECASE
            ):
                continue
            match = pattern.fullmatch(unit.text)
            if not match:
                continue
            if any(
                any(
                    e.locator == unit.locator and e.source_id == unit.source_id
                    for e in c.field_evidence.get("/charge_details/charge_item_raw", [])
                )
                for c in file.candidates
            ):
                continue
            name, raw = match["name"].strip(), match["currency"]
            amount = Decimal(match["amount"].replace(",", ""))
            evidence = Evidence(source_id=unit.source_id, locator=unit.locator, quote=unit.text)
            currency = raw if re.fullmatch(r"[A-Z]{3}", raw) else None
            keys = ["charge_item", "charge_item_raw", "amount", "currency_raw"] + (
                ["currency"] if currency else []
            )
            problems = [issue("EXPLICIT_EMAIL_ALLOCATION_REQUIRES_REVIEW")]
            if currency is None:
                problems.append(issue("CURRENCY_UNRESOLVED", "currency_raw"))
            file.candidates.append(
                ExtractedCandidate(
                    source_id=file.source_id,
                    source_line_id=file.source_id + ":line-" + digest([unit.unit_id, name])[:20],
                    document_info=DocumentInfo(document_type=None, reference_no=None, document_date=None),
                    transaction_party=TransactionParty(issuer=None, recipient=None),
                    charge_details=ChargeDetails(
                        charge_item=name,
                        charge_item_raw=name,
                        amount=amount,
                        currency=currency,
                        currency_raw=raw,
                        payment_term=None,
                    ),
                    evidence=[evidence],
                    field_evidence={"/charge_details/" + k: [evidence] for k in keys},
                    issues=list(file.issues) + problems,
                )
            )
            file.status = "pending_review"


def enrich_related_shipping(report, roots):
    """Join only missing literal shipping facts, with a shared explicit transport identity.

    No amounts, parties, currencies or fee rows are copied between sources.
    """
    patterns = {
        "vessel": r"(?:OCEAN\s+)?VESSEL(?: NAME)?\s*[:：]\s*(.+?)(?=\s+VOY(?:AGE)?\b|$)",
        "voyage": r"VOY(?:AGE)?(?: NO\.?)?\s*[:：]\s*(\S+)",
        "mbl_no": r"(?:MBL|MASTER B/L)(?: NO\.?)?\s*[:：]\s*(\S+)",
        "hbl_no": r"(?:HBL|HOUSE B/L)(?: NO\.?)?\s*[:：]\s*(\S+)",
    }
    for file in report.files:
        for candidate in file.candidates:
            own_units = [u for u in report.evidence_units if u.source_id == candidate.source_id]
            # Also retain explicit labeled transport IDs when a fee-only email rule was needed.
            for key in ("mbl_no", "hbl_no"):
                if key in candidate.shipping_info:
                    continue
                hits = [
                    (u, m.group(1).strip())
                    for u in own_units
                    if (m := re.search(patterns[key], u.text, re.IGNORECASE))
                ]
                if len({v for _, v in hits}) == 1:
                    unit, value = hits[0]
                    evidence = Evidence(source_id=unit.source_id, locator=unit.locator, quote=unit.text)
                    candidate.shipping_info[key] = value
                    candidate.field_evidence["/shipping_info/" + key] = [evidence]
                    candidate.evidence.append(evidence)
            anchors = []
            for key in ("mbl_no", "hbl_no", "booking_no", "container_nos"):
                val = candidate.shipping_info.get(key)
                anchors.extend(val if isinstance(val, list) else [val] if val else [])
            eligible = []
            for sid in roots:
                if sid == candidate.source_id or roots[sid] != roots[candidate.source_id]:
                    continue
                source_units = [u for u in report.evidence_units if u.source_id == sid]
                text = "\n".join(u.text for u in source_units)
                if any(
                    len(a) >= 6 and re.search(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", text)
                    for a in anchors
                ):
                    eligible.extend(source_units)
            for key, pattern in patterns.items():
                if key in candidate.shipping_info:
                    continue
                hits = [
                    (u, m.group(1).strip())
                    for u in eligible
                    if (m := re.search(pattern, u.text, re.IGNORECASE))
                ]
                if len({value for _, value in hits}) > 1:
                    candidate.issues.append(issue("RELATED_SHIPPING_CONTEXT_CONFLICT", key))
                    file.status = "pending_review"
                elif hits:
                    unit, value = hits[0]
                    evidence = Evidence(source_id=unit.source_id, locator=unit.locator, quote=unit.text)
                    candidate.shipping_info[key] = value
                    candidate.field_evidence["/shipping_info/" + key] = [evidence]
                    candidate.evidence.append(evidence)
