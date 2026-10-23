"""Project AR/AP policy: keep account category, sign credit/reversal amounts once."""

import re

from shipping_billing.domain.normalization import key, resolve


def document_kind(raw):
    token = re.sub(r"[\s_-]+", "", (raw or "").upper())
    if token in ("CREDITNOTE", "CREDITINVOICE", "CREDITMEMO", "贷项通知单", "贷项"):
        return "credit"
    if token in ("REVERSAL", "冲销"):
        return "reversal"
    if token in ("INVOICE", "DEBITINVOICE", "DEBITNOTE", "DEBITMEMO", "账单", "借项通知单"):
        return "normal"
    return "unknown"


def role_supported(roles, refs, index, source_id):
    if not index.verify(refs, None, source_id):
        return False
    text = "\n".join(r.quote for r in refs)
    for name, label in (
        (roles[0], r"PAYER|ORIGINAL PAYER|BILL TO|PAYABLE BY|PAID BY|原付款方|付款方|付款人"),
        (roles[1], r"PAYEE|ORIGINAL PAYEE|PAYABLE TO|PAID TO|原收款方|收款方|收款人"),
    ):
        if not re.search(
            r"(?:" + label + r")\s*[:：]\s*" + re.escape(name) + r"(?![\w])", text, re.IGNORECASE
        ):
            return False
    return True


def financial_direction(line, policy, context, aliases, index, add_issue, event):
    candidate = line.candidate
    kind = document_kind(candidate.document_info.document_type)
    amount = candidate.charge_details.amount
    entity = resolve(context.accounting_entity, aliases)
    line.financial_attributes.accounting_entity = entity
    event(
        "finance.explicit_entity",
        "/financial_attributes/accounting_entity",
        None,
        context.accounting_entity,
        (),
        "/accounting_entity",
    )
    event(
        "finance.entity_alias",
        "/financial_attributes/accounting_entity",
        context.accounting_entity,
        entity,
        (),
        "/party_aliases",
    )
    if entity is None:
        add_issue("MISSING_ACCOUNTING_ENTITY", "/financial_attributes/accounting_entity")
    roles, refs = None, []
    adjusted = (
        kind in ("credit", "reversal") or (amount is not None and amount < 0) or policy.adjustment is not None
    )
    if adjusted:
        fact = policy.adjustment
        if fact is None:
            add_issue("MISSING_ADJUSTMENT_RELATION", "/charge_details/amount")
            return
        text = "\n".join(e.quote for e in fact.evidence)
        supported = index.verify(fact.evidence, fact.original_reference, line.source_id)
        supported = supported and role_supported(
            (fact.original_payer, fact.original_payee), fact.evidence, index, line.source_id
        )
        vocabulary = {
            "credit": r"credit|贷项",
            "reversal": r"revers|冲销",
            "discount": r"discount|rebate|折扣|折让",
        }
        supported = supported and bool(re.search(vocabulary[fact.kind], text, re.IGNORECASE))
        if not supported:
            add_issue("UNSUPPORTED_ADJUSTMENT_RELATION", "/charge_details/amount")
            return
        if (
            kind in ("credit", "reversal") and kind != fact.kind
        ) or candidate.document_info.reference_no == fact.original_reference:
            add_issue("ADJUSTMENT_RELATION_CONFLICT", "/charge_details/amount")
            return
        if policy.settlement and (
            key(resolve(policy.settlement.payer, aliases)) != key(resolve(fact.original_payer, aliases))
            or key(resolve(policy.settlement.payee, aliases)) != key(resolve(fact.original_payee, aliases))
        ):
            add_issue("SETTLEMENT_ROLE_CONFLICT", "/financial_attributes/posting_type")
            return
        if policy.settlement and not role_supported(
            (policy.settlement.payer, policy.settlement.payee),
            policy.settlement.evidence,
            index,
            line.source_id,
        ):
            add_issue("UNSUPPORTED_SETTLEMENT_ROLES", "/financial_attributes/posting_type")
            return
        roles, refs = (fact.original_payer, fact.original_payee), fact.evidence
        if amount is not None:
            # copy_abs/copy_negate are exact even beyond the default Decimal precision.
            signed = amount.copy_abs().copy_negate() if amount else amount.copy_abs()
            event("amount.adjustment_sign_once", "/charge_details/amount", amount, signed, refs)
            candidate.charge_details.amount = signed
            line.balance_effect = "decrease" if signed else "none"
    elif policy.settlement:
        fact = policy.settlement
        if not role_supported((fact.payer, fact.payee), fact.evidence, index, line.source_id):
            add_issue("UNSUPPORTED_SETTLEMENT_ROLES", "/financial_attributes/posting_type")
            return
        roles, refs = (fact.payer, fact.payee), fact.evidence
    elif kind == "normal" and candidate.transaction_party.issuer and candidate.transaction_party.recipient:
        # Literal company names under shipping/bank roles do not prove billing roles.
        for field in ("issuer", "recipient"):
            raw_name = getattr(line.original.transaction_party, field)
            text = "\n".join(e.quote for e in candidate.field_evidence.get("/transaction_party/" + field, []))
            adverse = re.search(
                r"(?:SHIPPER|CONSIGNEE|NOTIFY PARTY|BENEFICIARY|BANK)\s*[:：]\s*" + re.escape(raw_name),
                text,
                re.IGNORECASE,
            )
            billing = re.search(
                r"(?:ISSUER|BILL TO|SUPPLIER|SELLER|签发方|接收方|收款方|付款方)\s*[:：]\s*"
                + re.escape(raw_name),
                text,
                re.IGNORECASE,
            )
            if adverse and not billing:
                add_issue("UNKNOWN_PARTY_ROLE", "/transaction_party/" + field)
                return
        roles = (candidate.transaction_party.recipient, candidate.transaction_party.issuer)
        refs = (
            candidate.field_evidence.get("/transaction_party/issuer", [])
            + candidate.field_evidence.get("/transaction_party/recipient", [])
            + candidate.field_evidence.get("/document_info/document_type", [])
        )
    if roles is None:
        add_issue("UNKNOWN_PARTY_ROLE", "/financial_attributes/posting_type")
        return
    payer, payee = (resolve(p, aliases) for p in roles)
    if key(payer) == key(payee):
        add_issue("SELF_TRANSACTION", "/financial_attributes/posting_type")
        return
    if line.balance_effect == "unknown" and amount is not None:
        line.balance_effect = "increase" if amount > 0 else "none"
    if entity is None:
        return
    posting = "AR" if key(entity) == key(payee) else "AP" if key(entity) == key(payer) else None
    if posting is None:
        add_issue("ACCOUNTING_ENTITY_NOT_PARTY", "/financial_attributes/posting_type")
        return
    line.financial_attributes.posting_type = posting
    event(
        "finance.original_relationship" if adjusted else "finance.billing_relationship",
        "/financial_attributes/posting_type",
        None,
        posting,
        refs,
        "/accounting_entity",
    )
