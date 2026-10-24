"""Reviewable reconciliation over immutable snapshots, with atomic per-record writes."""

from copy import deepcopy

from pydantic import ValidationError

from shipping_billing.domain import money
from shipping_billing.domain.engine import append_unique, normalize, problem
from shipping_billing.domain.evidence import EvidenceIndex
from shipping_billing.domain.finance import document_kind
from shipping_billing.domain.normalization import CHARGES, CURRENCIES, alias_map
from shipping_billing.errors import BillingError
from shipping_billing.extraction.engine import contains_value, digest
from shipping_billing.models import AuditTrail, BillingRecord, BillSnapshot, Change, Evidence, Locator
from shipping_billing.reconciliation.contracts import (
    DecisionFile,
    IdentityEntry,
    LedgerState,
    LineDecision,
    LineDirective,
    ReconciliationContext,
    ReconciliationReport,
)
from shipping_billing.reconciliation.intents import (
    FIELDS,
    REQUIRED,
    edit_valid,
    expected_supported,
    selector_valid,
)
from shipping_billing.reconciliation.matching import (
    equal,
    fee,
    hard_conflicts,
    level,
    occurrence,
    party,
    read,
    same_value,
    separate_adjustment,
    valid_money,
    write,
)

COMPARED = tuple(p for p in FIELDS if not p.endswith("_raw")) + (
    "/financial_attributes/accounting_entity",
    "/financial_attributes/posting_type",
)


def body(line):
    c = line.candidate
    return {
        "document_info": c.document_info.model_dump(mode="python"),
        "transaction_party": c.transaction_party.model_dump(mode="python"),
        "charge_details": c.charge_details.model_dump(mode="python"),
        "financial_attributes": line.financial_attributes.model_dump(mode="python"),
        "shipping_info": deepcopy(c.shipping_info),
        "extensions": {},
    }


def business(data):
    return {
        k: deepcopy(v) for k, v in data.items() if k not in ("operation", "existing_record_id", "audit_trail")
    }


def inherit_evidence(data, record_id):
    result = {}
    for group in (
        "document_info",
        "transaction_party",
        "charge_details",
        "financial_attributes",
        "shipping_info",
        "extensions",
    ):
        for field, value in data.get(group, {}).items():
            if value is not None:
                path = "/" + group + "/" + field
                from shipping_billing.io import dumps

                result[path] = [
                    Evidence(
                        source_id="existing:" + record_id,
                        locator=Locator(kind="existing_record", pointer=path),
                        quote=dumps(value),
                    )
                ]
    return result


def merged_valid(data, line, context):
    try:
        validated = BillSnapshot.model_validate(data)
    except ValidationError:
        return "INVALID_MERGED_RECORD"
    data.clear()
    data.update(validated.model_dump(mode="python", exclude_unset=True))
    if error := valid_money(data):
        return error
    entity = read(data, "/financial_attributes/accounting_entity")
    posting = read(data, "/financial_attributes/posting_type")
    if not entity or posting not in ("AR", "AP") or not read(data, "/charge_details/charge_item"):
        return "INVALID_MERGED_CORE"
    aliases = alias_map(context.normalization_context.party_aliases)
    a = party(read(data, "/transaction_party/issuer"), aliases)
    b = party(read(data, "/transaction_party/recipient"), aliases)
    e = party(entity, aliases)
    policy = context.normalization_context.lines.get(line.source_line_id)
    payer, payee = None, None
    if policy and policy.adjustment:
        payer = party(policy.adjustment.original_payer, aliases)
        payee = party(policy.adjustment.original_payee, aliases)
    elif policy and policy.settlement:
        payer = party(policy.settlement.payer, aliases)
        payee = party(policy.settlement.payee, aliases)
    elif document_kind(read(data, "/document_info/document_type")) == "normal":
        payer, payee = b, a
    if payer and payee:
        expected = "AR" if e == payee else "AP" if e == payer else None
        if payer == payee or expected != posting:
            return "MERGED_POSTING_CONFLICT"
    qty, rate = read(data, "/charge_details/quantity"), read(data, "/charge_details/unit_price")
    amount = read(data, "/charge_details/amount")
    if amount < 0 and (policy is None or policy.adjustment is None):
        return "MERGED_ADJUSTMENT_RELATION_MISSING"
    if amount > 0 and (
        (policy and policy.adjustment)
        or document_kind(read(data, "/document_info/document_type")) in ("credit", "reversal")
    ):
        return "MERGED_ADJUSTMENT_SIGN_CONFLICT"
    if qty is not None and rate is not None:
        # A stage 5 full line check already includes any evidence-backed tax/discount.
        unchanged_checked_line = all(
            read(data, "/charge_details/" + k) == getattr(line.candidate.charge_details, k)
            for k in ("quantity", "unit_price", "amount")
        )
        passed = any(c.code == "QUANTITY_PRICE" and c.status == "passed" for c in line.checks)
        if not (unchanged_checked_line and passed):
            expected = money.product(qty, rate)
            if amount < 0:
                expected = expected.copy_abs().copy_negate()
            policy = context.normalization_context.lines.get(line.source_line_id)
            rounding = (policy.rounding if policy else None) or context.normalization_context.rounding
            expected = money.expected(expected, CURRENCIES[read(data, "/charge_details/currency")], rounding)
            if expected != amount:
                return "MERGED_LINE_AMOUNT_MISMATCH"
    return None


def unresolved(line, old):
    if old is None:
        return list(line.issues)
    result = []
    raw = line.candidate.charge_details
    for issue in line.issues:
        resolved = (
            issue.code == "MISSING_AMOUNT"
            and raw.amount is None
            and read(old, "/charge_details/amount") is not None
        )
        resolved = resolved or (
            issue.code in ("MISSING_CURRENCY", "CURRENCY_UNRESOLVED")
            and raw.currency_raw is None
            and raw.currency is None
            and read(old, "/charge_details/currency") is not None
        )
        resolved = resolved or (
            issue.code == "UNKNOWN_PARTY_ROLE"
            and (
                line.candidate.transaction_party.issuer is None
                or line.candidate.transaction_party.recipient is None
            )
            and line.financial_attributes.accounting_entity is not None
            and read(old, "/financial_attributes/posting_type") in ("AR", "AP")
            and read(old, "/transaction_party/issuer") is not None
            and read(old, "/transaction_party/recipient") is not None
        )
        if not resolved:
            result.append(issue)
    return result


def make_record(plan, context_hash):
    line, decision = plan["line"], plan["decision"]
    data = deepcopy(
        plan["desired"]
        if decision.operation in ("CREATE", "UPDATE", "NO_CHANGE")
        else plan["old"] or plan["new"]
    )
    data["record_id"] = decision.record_id
    refs = (
        inherit_evidence(data, decision.record_id) if plan["old"] else deepcopy(line.candidate.field_evidence)
    )
    if decision.operation == "UPDATE":
        for change in decision.proposed_changes:
            refs[change.path] = change.evidence
    context_id = "context:" + context_hash
    if not plan["old"] and line.financial_attributes.accounting_entity:
        refs["/financial_attributes/accounting_entity"] = [
            Evidence(
                source_id=context_id,
                locator=Locator(kind="existing_record", pointer="/normalization_context/accounting_entity"),
                quote=line.financial_attributes.accounting_entity,
            )
        ]
        refs["/financial_attributes/posting_type"] = [
            e
            for event in line.events
            if event.path == "/financial_attributes/posting_type"
            for e in event.evidence
        ]
    sources = sorted(
        {e.source_id for values in refs.values() for e in values}
        | {e.source_id for c in decision.proposed_changes for e in c.evidence}
    )
    reasons = [
        "Rule " + (decision.match_rule or "unmatched") + ": " + decision.operation,
        "Only explicit supported edits apply; missing values preserve the existing snapshot.",
    ]
    data.update(
        operation=decision.operation,
        existing_record_id=decision.record_id if plan["old"] else None,
        audit_trail=AuditTrail(
            ai_reasoning=reasons,
            sources=sources,
            field_evidence=refs,
            issues=decision.issues,
            changes=decision.proposed_changes if decision.operation == "UPDATE" else [],
        ).model_dump(mode="python"),
    )
    return BillingRecord.model_validate(data)


def reconcile(normalized, extraction, *, state=None, context=None, parsed=None):
    state, context = state or LedgerState(), context or ReconciliationContext()
    if normalize(extraction, context.normalization_context) != normalized:
        raise BillingError(
            "NORMALIZATION_INPUT_MISMATCH",
            "Normalization must be reproduced from its exact evidence and context.",
        )
    index = EvidenceIndex(extraction)
    source_hashes = {}
    if parsed is not None:
        if digest(parsed.model_dump(mode="python")) != extraction.input_sha256:
            raise BillingError("PARSE_INPUT_MISMATCH", "Source hashes must belong to the extraction input.")
        source_hashes = {s.source_id: s.sha256 for s in parsed.sources if s.sha256}
    if set(context.lines) - {line.source_line_id for line in normalized.lines}:
        raise BillingError("UNKNOWN_CONTEXT_LINE", "Directive references an unknown source line.")
    charges = alias_map({**CHARGES, **context.normalization_context.charge_aliases})
    parties = alias_map(context.normalization_context.party_aliases)
    original_bills = {b.record_id: b.model_dump(mode="python", exclude_unset=True) for b in state.bills}
    pool = deepcopy(original_bills)
    identities = {entry.record_id: set(entry.occurrence_keys) for entry in state.identities}
    occurrences = {k: entry.record_id for entry in state.identities for k in entry.occurrence_keys}
    reviewed = {}
    for event in state.audit_log:
        if event.get("kind") == "human_review" and event.get("action") in ("create", "update", "duplicate"):
            for entry in event.get("occurrences", []):
                reviewed[entry["key"]] = (entry["observed_sha256"], event["target_record_id"], event["decision_id"])
    created = {}
    plans = []
    for line in normalized.lines:
        incoming = body(line)
        directive = context.lines.get(line.source_line_id, LineDirective())
        problems = []
        edits = {}
        for edit in directive.edits:
            if error := edit_valid(edit, line, index):
                problems.append(problem(error, edit.path))
            else:
                edits[edit.path] = edit
        lookup = deepcopy(incoming)
        for path, edit in edits.items():
            if edit.expected_old is not None and expected_supported(edit, line, index):
                write(lookup, path, edit.expected_old)
        physical, document = occurrence(line, extraction, source_hashes)
        previous_review = reviewed.get(physical)
        if previous_review and previous_review[0] == digest(incoming) and occurrences.get(physical) == previous_review[1]:
            reviewed_id = previous_review[1]
            reviewed_bill = deepcopy(pool[reviewed_id])
            # Exact original bytes, physical row and observed facts already have a human receipt.
            # Reuse that decision without turning the stale OCR observation into a new edit.
            if (not valid_money(reviewed_bill) and read(reviewed_bill, "/charge_details/charge_item")
                and read(reviewed_bill, "/financial_attributes/posting_type") in ("AR", "AP")
                and party(read(reviewed_bill, "/financial_attributes/accounting_entity"), parties)
                == party(context.normalization_context.accounting_entity, parties)):
                plans.append(dict(
                    line=line, old=reviewed_bill, new=incoming, desired=deepcopy(reviewed_bill),
                    physical=physical, document=document,
                    decision=LineDecision(source_line_id=line.source_line_id, source_id=line.source_id,
                                          operation="NO_CHANGE", record_id=reviewed_id,
                                          matched_record_ids=[reviewed_id],
                                          match_rule="human_review:" + previous_review[2], observed=incoming),
                ))
                continue

        def compatible(old):
            view = deepcopy(incoming)
            for path, edit in edits.items():
                if equal(read(old, path), edit.expected_old) and expected_supported(edit, line, index):
                    write(view, path, edit.expected_old)
            return not hard_conflicts(view, old, parties)

        matched, rule, collisions = [], None, []
        if physical in occurrences:
            matched, rule = [occurrences[physical]], "source_occurrence"
        else:
            ranked = {}
            for rid, old in pool.items():
                if rid in created and created[rid] == document:
                    continue  # another physical row in this same document is not a duplicate
                if separate_adjustment(lookup, old):
                    continue
                tiers = [
                    n for view in (lookup, incoming) if (n := level(view, old, charges, parties)) is not None
                ]
                tier = min(tiers) if tiers else None
                if tier is not None:
                    ranked.setdefault(tier, []).append(rid)
            if ranked:
                first = min(ranked)
                collisions = sorted(ranked[first])
                matched = [rid for rid in collisions if compatible(pool[rid])]
                rule = "priority_" + str(first)
        if matched and any(not compatible(pool[rid]) for rid in matched):
            problems.append(problem("MATCH_HARD_CONFLICT"))
            matched = []
        if collisions and not matched:
            problems.append(problem("MATCH_HARD_CONFLICT"))
        if directive.selector:
            if not selector_valid(directive.selector, line, index):
                problems.append(problem("UNSUPPORTED_RECORD_SELECTOR"))
                matched = []
            elif directive.selector.record_id not in matched:
                problems.append(problem("REQUESTED_TARGET_NOT_MATCHED"))
                matched = []
            else:
                matched = [directive.selector.record_id]
                rule = (rule or "") + "+explicit_record"
        if len(matched) > 1:
            problems.append(problem("MULTIPLE_MATCHES"))
        rid = matched[0] if len(matched) == 1 else None
        old = deepcopy(pool[rid]) if rid else None
        problems += unresolved(line, old)
        desired = deepcopy(old or incoming)
        changes, differences = [], []
        if old:
            for path in COMPARED:
                new_value, old_value = read(incoming, path), read(old, path)
                if new_value is not None and not same_value(path, new_value, old_value, charges, parties):
                    differences.append(path)
                    if path not in edits:
                        problems.append(problem("UNAUTHORIZED_CHANGE", path))
            for path, edit in edits.items():
                value = read(old, path)
                if equal(value, edit.value):
                    continue  # repeated supported request after application is already satisfied
                if not equal(value, edit.expected_old):
                    problems.append(problem("STALE_EXPECTED_VALUE", path))
                    continue
                observed = read(incoming, path)
                if (
                    edit.action == "set"
                    and not path.startswith("/extensions/")
                    and observed is not None
                    and not equal(observed, edit.value)
                ):
                    problems.append(problem("INTENT_VALUE_CONFLICT", path))
                    continue
                try:
                    write(desired, path, deepcopy(edit.value))
                except ValueError:
                    problems.append(problem("INVALID_EDIT_STRUCTURE", path))
                    continue
                changes.append(Change(path=path, old=value, new=edit.value, evidence=edit.evidence))
        else:
            if directive.edits or directive.selector:
                problems.append(problem("UPDATE_TARGET_NOT_FOUND"))
            if not physical:
                problems.append(problem("INSUFFICIENT_LINE_IDENTITY"))
            # An old same-fee/amount row without sufficient keys may be a duplicate: do not guess.
            weak = [
                b
                for b in original_bills.values()
                if fee(incoming, charges) is not None
                and fee(incoming, charges) == fee(b, charges)
                and read(incoming, "/charge_details/amount") == read(b, "/charge_details/amount")
                and read(incoming, "/charge_details/currency") == read(b, "/charge_details/currency")
                and party(read(incoming, "/financial_attributes/accounting_entity"), parties)
                == party(read(b, "/financial_attributes/accounting_entity"), parties)
                and not separate_adjustment(incoming, b)
                and not (
                    read(incoming, "/document_info/reference_no")
                    and read(b, "/document_info/reference_no")
                    and read(incoming, "/document_info/reference_no")
                    != read(b, "/document_info/reference_no")
                )
            ]
            if weak and not matched and not collisions:
                problems.append(problem("WEAK_DUPLICATE_CANDIDATES"))
        rid = (
            rid
            or "bill-"
            + digest([physical or line.source_line_id, line.financial_attributes.accounting_entity])[:24]
        )
        desired["record_id"] = rid
        if error := merged_valid(desired, line, context):
            problems.append(problem(error))
        unique_problems = []
        for item in problems:
            append_unique(unique_problems, item)
        operation = (
            "PENDING_REVIEW"
            if any(p.blocks_automation for p in unique_problems)
            else "UPDATE"
            if old and changes
            else "NO_CHANGE"
            if old
            else "CREATE"
        )
        decision = LineDecision(
            source_line_id=line.source_line_id,
            source_id=line.source_id,
            operation=operation,
            record_id=rid,
            matched_record_ids=matched or collisions,
            match_rule=rule,
            issues=unique_problems,
            differences=differences,
            proposed_changes=changes,
            observed=incoming,
        )
        plan = {
            "line": line,
            "decision": decision,
            "old": old,
            "new": incoming,
            "desired": desired,
            "physical": physical,
            "document": document,
        }
        plans.append(plan)
        if operation == "CREATE":
            pool[rid] = deepcopy(desired)
            created[rid] = document
            if physical:
                occurrences[physical] = rid
    return finalize(plans, normalized, extraction, context, state, original_bills, identities)


def finalize(plans, normalized, extraction, context, state, originals, identities):
    grouped = {}
    for plan in plans:
        grouped.setdefault(plan["decision"].record_id, []).append(plan)
    records = []
    for rid, members in grouped.items():
        conflict = len({digest(business(p["desired"])) for p in members}) > 1
        conflict = conflict or (
            len(members) > 1 and any(p["decision"].operation == "PENDING_REVIEW" for p in members)
        )
        if conflict:
            for plan in members:
                plan["decision"].operation = "PENDING_REVIEW"
                append_unique(plan["decision"].issues, problem("BATCH_TARGET_CONFLICT"))
        first = members[0]
        record = make_record(first, digest(context.model_dump(mode="python")))
        for other in members[1:]:
            if not conflict:
                other["decision"].operation = "NO_CHANGE"
                other["decision"].duplicate_of = first["line"].source_line_id
                for path, refs in other["line"].candidate.field_evidence.items():
                    record.audit_trail.field_evidence.setdefault(path, []).extend(
                        e
                        for e in refs
                        if contains_value(read(record.model_dump(mode="python"), path), e.quote)
                        and e not in record.audit_trail.field_evidence.get(path, [])
                    )
                record.audit_trail.sources = sorted(
                    set(record.audit_trail.sources)
                    | {e.source_id for refs in other["line"].candidate.field_evidence.values() for e in refs}
                )
        records.append(record)
    bills = apply_records(list(originals.values()), records)
    accepted = {r.record_id for r in records if r.operation != "PENDING_REVIEW"}
    for plan in plans:
        rid = plan["decision"].record_id
        if rid in accepted and plan["physical"]:
            identities.setdefault(rid, set()).add(plan["physical"])
    audit = deepcopy(state.audit_log)
    for record in records:
        if record.operation in ("CREATE", "UPDATE"):
            audit.append(
                {
                    "record_id": record.record_id,
                    "operation": record.operation,
                    "audit_trail": record.audit_trail.model_dump(mode="python"),
                }
            )
    next_state = LedgerState.model_validate(
        {
            "bills": bills,
            "identities": [
                IdentityEntry(record_id=rid, occurrence_keys=sorted(keys)) for rid, keys in identities.items()
            ],
            "audit_log": audit,
        }
    )
    files = []
    for file in normalized.files:
        decisions = [p["decision"] for p in plans if p["line"].source_id == file.source_id]
        status = (
            "error"
            if file.status == "error"
            else "pending_review"
            if any(d.operation == "PENDING_REVIEW" for d in decisions)
            or file.status == "pending_review"
            and not decisions
            else "processed"
            if decisions
            else "no_charge"
        )
        files.append(
            DecisionFile(
                source_id=file.source_id,
                parent_source_id=file.parent_source_id,
                status=status,
                issues=file.issues,
            )
        )
    return ReconciliationReport(
        input_sha256=digest(normalized.model_dump(mode="python")),
        previous_state_sha256=digest(state.model_dump(mode="python")),
        records=records,
        decisions=[p["decision"] for p in plans],
        files=files,
        next_state=next_state,
    )


def apply_records(existing, records):
    bills = {b["record_id"]: deepcopy(b) for b in existing}
    for record in records:
        if record.operation in ("NO_CHANGE", "PENDING_REVIEW"):
            continue
        data = record.model_dump(mode="python", exclude_unset=True)
        if record.operation == "CREATE":
            if record.record_id in bills:
                raise BillingError("DUPLICATE_RECORD_CREATE", "A CREATE must not replace an existing record.")
            bills[record.record_id] = business(data)
        else:
            if record.record_id not in bills:
                raise BillingError("UPDATE_TARGET_NOT_FOUND", "Cannot update a missing record.")
            updated = deepcopy(bills[record.record_id])
            for change in record.audit_trail.changes:
                if change.path not in FIELDS and not change.path.startswith("/extensions/"):
                    raise BillingError("UNSUPPORTED_EDIT_PATH", "Snapshot plan targets a protected field.")
                if change.new is None and change.path in REQUIRED:
                    raise BillingError("REQUIRED_FIELD_CLEAR_FORBIDDEN", "Core fields cannot be cleared.")
                if not equal(read(updated, change.path), change.old):
                    raise BillingError("STALE_SNAPSHOT", "The snapshot changed after the plan was built.")
                write(updated, change.path, deepcopy(change.new))
            # Unmodified root and nested custom fields must survive byte-for-value.
            expected = business(updated)
            actual = business(data)
            if not equal(expected, actual):
                raise BillingError(
                    "UNAUTHORIZED_RECORD_DIFF", "Record contains changes outside its explicit plan."
                )
            bills[record.record_id] = updated
    return list(bills.values())
