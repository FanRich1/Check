"""Typed intents still need literal, field-specific business modification evidence."""

import re
from decimal import Decimal

from shipping_billing.extraction.engine import contains_value
from shipping_billing.reconciliation.matching import read

FIELDS = {
    "/charge_details/amount": r"amount|金额",
    "/charge_details/currency": r"currency|币种",
    "/charge_details/charge_item": r"charge[_ ]?item|fee name|费用名|费用项",
    "/charge_details/payment_term": r"payment[_ ]?term|payment|付款|支付方式",
    "/charge_details/payment_term_raw": r"payment[_ ]?term|payment|付款|支付方式",
    "/charge_details/quantity": r"quantity|qty|数量",
    "/charge_details/unit_price": r"unit[_ ]?price|rate|单价",
    "/charge_details/charge_item_raw": r"charge[_ ]?item|fee name|费用名|费用项",
    "/charge_details/currency_raw": r"currency|币种",
    "/document_info/reference_no": r"reference[_ ]?no|invoice[_ ]?(?:no|number)|单据号|单据编号",
    "/document_info/document_type": r"document[_ ]?type|单据类型",
    "/document_info/document_date": r"document[_ ]?date|invoice[_ ]?date|单据日期",
    "/document_info/document_date_raw": r"document[_ ]?date|invoice[_ ]?date|单据日期",
    "/transaction_party/issuer": r"issuer|签发方",
    "/transaction_party/recipient": r"recipient|bill[_ ]?to|接收方",
    "/shipping_info/mbl_no": r"mbl|master b/l|主单",
    "/shipping_info/hbl_no": r"hbl|house b/l|分单",
    "/shipping_info/booking_no": r"booking|订舱",
    "/shipping_info/container_nos": r"container|cntr|箱号",
    "/shipping_info/vessel": r"vessel|船名",
    "/shipping_info/voyage": r"voyage|航次",
    "/shipping_info/port_of_loading": r"port.of.loading|起运港",
    "/shipping_info/port_of_discharge": r"port.of.discharge|目的港",
    "/shipping_info/arrival_date_raw": r"arrival.date|eta|到港日期",
}
REQUIRED = {"/charge_details/amount", "/charge_details/currency", "/charge_details/charge_item"}


def intent_value(value, text):
    if isinstance(value, list):
        return all(intent_value(item, text) for item in value)
    if isinstance(value, bool):
        return bool(re.search(r"\b" + str(value).lower() + r"\b", text, re.IGNORECASE))
    if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool):
        tokens = re.findall(r"(?<![\d.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?!\d)", text)
        return Decimal(str(value)) in [Decimal(t.replace(",", "")) for t in tokens]
    return contains_value(value, text)


def expected_supported(edit, line, index):
    return index.verify(edit.evidence, None, line.source_id) and intent_value(
        edit.expected_old, "\n".join(e.quote for e in edit.evidence)
    )


def selector_valid(selector, line, index):
    if not index.verify(selector.evidence, selector.record_id, line.source_id):
        return False
    text = "\n".join(e.quote for e in selector.evidence)
    return bool(
        re.search(
            r"(?:record|record_id|记录|账单|费用行)\s*[:：#]?\s*" + re.escape(selector.record_id) + r"(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


def edit_valid(edit, line, index):
    label = FIELDS.get(edit.path)
    if label is None and edit.path.startswith("/extensions/"):
        parts = edit.path.split("/")[2:]
        if not parts or any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
            return "UNSUPPORTED_EDIT_PATH"
        label = re.escape(parts[-1])
    if label is None:
        return "UNSUPPORTED_EDIT_PATH"
    if not index.verify(edit.evidence, None, line.source_id):
        return "UNSUPPORTED_INTENT_EVIDENCE"
    quotes = [e.quote for e in edit.evidence]
    forbidden = r"ignore.*(?:rule|system)|忽略.*(?:规则|系统)|system prompt|\b(?:do not|don't|must not|never|no need to)\b|不要|不得|禁止|无需|不允许"
    if any(re.search(forbidden, q, re.IGNORECASE) for q in quotes):
        return "EXPLICIT_MODIFICATION_NOT_SUPPORTED"
    # A modification in another clause cannot authorize an observed field value.
    for text in [part for quote in quotes for part in re.split(r"[;；\n。]", quote)]:
        if not re.search(label, text, re.IGNORECASE):
            continue
        if edit.action == "clear":
            if edit.path in REQUIRED:
                return "REQUIRED_FIELD_CLEAR_FORBIDDEN"
            if re.search(r"\b(clear|remove|unset|empty)\b|清空|置空|删除", text, re.IGNORECASE):
                return None
        elif re.search(
            r"\b(change|update|set|replace|correct)\b|修改|更改|改为|调整|更新|设为|修正", text, re.IGNORECASE
        ):
            if intent_value(edit.value, text):
                return None
            candidate = line.candidate.model_dump(mode="python")
            if read(candidate, edit.path) == edit.value:
                raw = {
                    "/charge_details/charge_item": "charge_item_raw",
                    "/charge_details/currency": "currency_raw",
                    "/charge_details/payment_term": "payment_term_raw",
                }.get(edit.path)
                if (
                    raw
                    and getattr(line.candidate.charge_details, raw)
                    and getattr(line.candidate.charge_details, raw) in text
                ):
                    return None
    return "EXPLICIT_MODIFICATION_NOT_SUPPORTED"
