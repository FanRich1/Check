"""Explicit human decisions over immutable observations; no model or OCR calls."""

import re
from copy import deepcopy
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, StrictBool, ValidationError, field_validator

from shipping_billing.domain import money
from shipping_billing.domain.normalization import CHARGES, alias_map, currency, party_key, resolve
from shipping_billing.errors import BillingError
from shipping_billing.extraction.engine import digest
from shipping_billing.models import BillSnapshot, Contract, same_json_value
from shipping_billing.reconciliation.contracts import IdentityEntry, LedgerState
from shipping_billing.reconciliation.matching import read, valid_money, write

FIELDS = (
    "/document_info/document_type", "/document_info/reference_no", "/document_info/document_date",
    "/transaction_party/issuer", "/transaction_party/recipient", "/charge_details/charge_item",
    "/charge_details/amount", "/charge_details/currency", "/charge_details/payment_term",
    "/charge_details/quantity", "/charge_details/unit_price",
)
ACTIONS = {"create": "确认新增", "update": "修改旧账", "duplicate": "确认重复", "reject": "拒绝入账"}


class ReviewRequest(Contract):
    record_id: Annotated[str, Field(min_length=1, max_length=300)]
    ledger_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    revision: Annotated[int, Field(strict=True, ge=0)]
    action: Literal["create", "update", "duplicate", "reject"]
    target_record_id: Annotated[str, Field(min_length=1, max_length=300)] | None = None
    fields: dict[str, Annotated[str, Field(max_length=500)] | None] = Field(default_factory=dict)
    payer: Annotated[str, Field(max_length=300)] = ""
    payee: Annotated[str, Field(max_length=300)] = ""
    reviewer: Annotated[str, Field(min_length=1, max_length=100)]
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    distinct: StrictBool = False

    @field_validator("reviewer", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("reviewer and reason must not be blank")
        return value.strip()

    @field_validator("fields")
    @classmethod
    def allowed_fields(cls, value):
        if set(value) - set(FIELDS):
            raise ValueError("protected or unknown review fields")
        return value


def request_hash(request):
    return digest(request.model_dump(mode="python"))


def form_values(bill):
    return {path: None if read(bill, path) is None else str(read(bill, path)) for path in FIELDS}


def visible_bill(bill):
    if bill is None:
        return None
    amount = read(bill, "/charge_details/amount")
    return {**bill, "amount_display": "—" if amount is None else str(amount), "form_values": form_values(bill)}


def build_plan(state, book, origin, request):
    """The same pure plan is rebuilt under the database lock before committing."""
    if request.revision != book["revision"]:
        raise BillingError("LEDGER_CHANGED", "账本已更新，请重新打开复审并预览最新差异。")
    target = next((b.model_dump(mode="python", exclude_unset=True) for b in state.bills
                   if b.record_id == request.target_record_id), None)
    if request.action in ("update", "duplicate") and target is None:
        raise BillingError("REVIEW_TARGET_REQUIRED", "请选择当前账本中真实存在的目标旧账。")
    if request.action in ("create", "reject") and request.target_record_id is not None:
        raise BillingError("INVALID_REVIEW_DECISION", "新增或拒绝时不能指定旧账目标。")
    if request.action in ("duplicate", "reject") and request.fields:
        raise BillingError("INVALID_REVIEW_DECISION", "重复或拒绝决定不能携带账单修改。")
    if request.action == "create" and not request.distinct:
        raise BillingError("REVIEW_DISTINCT_REQUIRED", "请明确确认这是一笔独立的新费用。")
    before, after = deepcopy(target), deepcopy(target)
    if request.action in ("create", "update"):
        if request.action == "create":
            after = deepcopy(origin["new_bill"])
            after["record_id"] = "manual-" + digest([origin["job_id"], request.record_id, book["id"]])[:24]
            for name in ("operation", "existing_record_id", "audit_trail"):
                after.pop(name, None)
        for path, raw in request.fields.items():
            value = raw.strip() if isinstance(raw, str) else None
            value = value or None
            if path in ("/charge_details/amount", "/charge_details/quantity", "/charge_details/unit_price") and value is not None:
                if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
                    raise BillingError("INVALID_REVIEW_AMOUNT", "请输入普通十进制金额，不含逗号或货币符号。")
                value = Decimal(value)
            write(after, path, value)
        fee = read(after, "/charge_details/charge_item")
        write(after, "/charge_details/charge_item", resolve(fee, alias_map(CHARGES)))
        code, error = currency(read(after, "/charge_details/currency"))
        if error:
            raise BillingError("INVALID_REVIEW_CURRENCY", "请提供明确有效的币种，例如 USD、CNY。")
        write(after, "/charge_details/currency", code)
        payer, payee = request.payer.strip(), request.payee.strip()
        if not payer or not payee or party_key(payer) == party_key(payee):
            raise BillingError("REVIEW_PARTIES_REQUIRED", "请确认不同的付款方和收款方。")
        posting = (
            "AP" if party_key(book["entity"]) == party_key(payer)
            else "AR" if party_key(book["entity"]) == party_key(payee) else None
        )
        if posting is None:
            raise BillingError("REVIEW_ENTITY_NOT_PARTY", "记账公司必须是已确认的付款方或收款方之一。")
        after["financial_attributes"] = {**after.get("financial_attributes", {}),
                                         "accounting_entity": book["entity"], "posting_type": posting}
        try:
            after = BillSnapshot.model_validate(after).model_dump(mode="python", exclude_unset=True)
        except ValidationError:
            raise BillingError("INVALID_REVIEW_BILL", "复审字段格式无效，请检查日期、付款方式和金额等字段。") from None
        if not read(after, "/charge_details/charge_item") or valid_money(after):
            raise BillingError("INVALID_REVIEW_BILL", "费用名称、金额或币种精度未通过校验，请修正后预览。")
        qty, rate = read(after, "/charge_details/quantity"), read(after, "/charge_details/unit_price")
        if qty is not None and rate is not None:
            amount = read(after, "/charge_details/amount")
            proof = any(all(read(after, "/charge_details/" + field) == line["values"].get(field)
                            for field in ("amount", "quantity", "unit_price")) for line in origin["checked_lines"])
            if not proof and money.product(qty, rate) != amount:
                raise BillingError("REVIEW_LINE_AMOUNT_MISMATCH", "数量乘单价与金额不符，请修正；含税费折扣时需补充完整计价依据后重新处理。")
    if request.action == "reject":
        after = None
    target_id = after["record_id"] if after else None
    # A source already assigned to an accepted bill cannot be silently reassigned or created again.
    owners = {k: entry.record_id for entry in state.identities for k in entry.occurrence_keys}
    for entry in origin["occurrences"]:
        owner = owners.get(entry["key"])
        if request.action != "reject" and owner and (request.action == "create" or owner != target_id):
            raise BillingError("REVIEW_SOURCE_ALREADY_BOOKED", "该来源已对应其他有效账单，请核对并选择正确旧账。")
    changes = []
    for path in (*FIELDS, "/financial_attributes/accounting_entity", "/financial_attributes/posting_type"):
        old, new = read(before or {}, path), read(after or {}, path)
        if not same_json_value(old, new):
            changes.append(dict(path=path, old=old, new=new,
                                old_display="—" if old is None else str(old),
                                new_display="—" if new is None else str(new)))
    if request.action == "update" and not changes:
        raise BillingError("REVIEW_NO_CHANGES", "没有可应用的修改；如需保留旧值，请选择确认重复。")
    plan = dict(job_id=origin["job_id"], record_id=request.record_id, ledger_id=book["id"],
                action=request.action, action_label=ACTIONS[request.action], target_record_id=target_id,
                reviewer=request.reviewer, reason=request.reason, payer=request.payer.strip(), payee=request.payee.strip(),
                base_revision=book["revision"], before=before, after=after, changes=changes,
                original=origin["original"], observations=origin["observations"],
                related_fields=origin.get("related_fields", []),
                occurrences=origin["occurrences"], acknowledged_issues=origin["issues"],
                request_sha256=request_hash(request))
    plan["preview_token"] = digest(plan)
    next_state = state.model_copy(deep=True)
    if request.action == "create":
        next_state.bills.append(BillSnapshot.model_validate(after))
    elif request.action == "update":
        next_state.bills = [BillSnapshot.model_validate(after) if b.record_id == target_id else b for b in state.bills]
    if target_id:
        keys = {e["key"] for e in origin["occurrences"]}
        identity = next((entry for entry in next_state.identities if entry.record_id == target_id), None)
        if identity is None:
            next_state.identities.append(IdentityEntry(record_id=target_id, occurrence_keys=sorted(keys)))
        else:
            identity.occurrence_keys = sorted(set(identity.occurrence_keys) | keys)
    return plan, LedgerState.model_validate(next_state.model_dump(mode="python"))


def visible_plan(plan):
    observed = plan["observations"][0]["observed"] if plan["observations"] else plan["original"]
    return {**plan, "before": visible_bill(plan["before"]), "after": visible_bill(plan["after"]),
            "display_bill": visible_bill(plan["after"] if plan["after"] is not None else observed)}
