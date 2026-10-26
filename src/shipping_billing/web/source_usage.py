"""Current document-purpose views over immutable parsing and extraction evidence."""

import re

from pydantic import Field, StrictBool, field_validator, model_validator

from shipping_billing.models import Contract
from shipping_billing.pipeline.source_status import IMAGES as IMAGES
from shipping_billing.pipeline.source_status import PURPOSE_ONLY, empty_image_requires_confirmation


class SourceReviewRequest(Contract):
    source_id: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    classification: str = Field(pattern=r"^(non_billing_image|supporting_document|reopen)$")
    revision: int = Field(strict=True, ge=0)
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    confirmed: StrictBool

    @field_validator("reviewer", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("reviewer and reason must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("source purpose confirmation is required")
        return self


def native_transport_support(source, extracted):
    """Recognize fully read transport material, never merely a zero-fee extraction."""
    if source.get("detected_format") != "pdf" or extracted.get("candidates") or extracted.get("status") == "error":
        return []
    metadata = source.get("metadata", {})
    pages = metadata.get("pages", 0)
    native = [f for f in source.get("fragments", []) if f.get("method") == "pdf_native_text"]
    if (not pages or {f["locator"].get("page") for f in native if f["text"].strip()} != set(range(1, pages + 1))
            or {p["page"] for p in metadata.get("ocr_page_status", []) if p["status"] == "completed"}
            != set(range(1, pages + 1))):
        return []
    text = "\n".join(f["text"] for f in native)
    # An invoice containing a B/L reference must not be mistaken for a supporting B/L.
    if re.search(r"\b(?:INVOICE|DEBIT\s+NOTE|CREDIT\s+NOTE)\b|账单|发票", text, re.I):
        return []
    if not (re.search(r"\b(?:BILL\s+OF\s+LADING|SEA\s+WAYBILL)\b", text, re.I)
            and re.search(r"\bSHIPPER\b", text, re.I) and re.search(r"\bCONSIGNEE\b", text, re.I)
            and re.search(r"\b(?:AS\s+ARRANGE+D|FREIGHT\s+(?:PREPAID|COLLECT))\b", text, re.I)):
        return []
    from shipping_billing.domain.normalization import CURRENCIES
    codes = "|".join(sorted(CURRENCIES))
    # KGS after a number is the weight unit on a B/L. A labeled currency or price still vetoes classification.
    trailing_codes = "|".join(sorted(set(CURRENCIES) - {"KGS"}))
    money = r"(?:[$￥€£]\s*\d|\b(?:" + codes + r")\s*[+-]?\d|\d(?:[\d,.]*\d)?\s*(?:" + trailing_codes + r")\b)"
    if re.search(money, text, re.I):
        return []
    if re.search(r"\bCURRENCY\b|(?<![\d.])\d+\.\d{2}(?!\d)", text, re.I):
        return []
    matches = []
    for fragment in native:
        for pattern in (r"BILL\s+OF\s+LADING|SEA\s+WAYBILL", r"AS\s+ARRANGE+D|FREIGHT\s+(?:PREPAID|COLLECT)"):
            if match := re.search(pattern, fragment["text"], re.I):
                matches.append(dict(source_id=source["source_id"], locator=fragment["locator"], quote=match[0]))
    return matches


def assess_sources(files, parsed, extraction, records, fee_decisions, histories):
    sources = {s["source_id"]: s for s in parsed.get("sources", [])}
    extracted = {f["source_id"]: f for f in extraction["files"]}
    by_record = {r["record_id"]: r for r in records}
    result = []
    for file in files:
        sid = file["source_id"]
        source, facts = sources.get(sid, {}), extracted.get(sid, {})
        history = histories.get(sid, [])
        latest = history[-1] if history else None
        confirmed = latest and latest["classification"] != "reopen"
        codes = {i["code"] for i in file["issues"] if i.get("blocks_automation", True)}
        owned = [by_record[rid] for rid in file.get("record_ids", []) if rid in by_record]
        has_fees = bool(owned or facts.get("candidates"))
        empty_image = empty_image_requires_confirmation(source, has_fees=has_fees, issue_codes=codes)
        hard = (source.get("status") == "error" or (facts.get("status") == "error" and not empty_image) or
                (file["status"] == "error" and bool(codes - PURPOSE_ONLY)) or
                any(p["status"] == "error" and not empty_image
                    for p in source.get("metadata", {}).get("ocr_page_status", [])))
        pending = sum(r["operation"] == "PENDING_REVIEW" and r["record_id"] not in fee_decisions for r in owned)
        evidence = native_transport_support(source, facts) if not hard and not has_fees else []
        use = dict(role="unknown", status="purpose_pending", label="用途待确认", basis="unknown",
                   reading="需核对原件", note="尚不能仅凭零条费用判断材料用途，请查看原件后确认。",
                   evidence=[], can_confirm=not hard and not has_fees and file.get("detected_format") != "eml",
                   revision=latest["revision"] if latest else 0, review=latest)
        if empty_image:
            use.update(reading="识别调用已完成，未返回账单内容",
                       note="请查看原图确认用途：如为宣传图或签名图，可确认非账单图片；如含费用，请补充清晰材料重新识别。")
        if hard:
            use.update(status="error", label="识别失败，需处理", reading="读取或识别未完成",
                       note="请先处理文件读取或识别故障，材料用途确认不能替代此项处理。", can_confirm=False)
        elif has_fees:
            use.update(role="billing", status="fee_pending" if pending else "fee_processed",
                       label="费用待复核" if pending else "费用已复核"
                       if any(r["record_id"] in fee_decisions for r in owned) else "费用已处理", reading="已取得费用证据",
                       note=f"本来源已识别的费用中，剩余 {pending} 条待复核。", can_confirm=False)
        elif confirmed and latest["sha256"] == file.get("sha256"):
            image = latest["classification"] == "non_billing_image"
            use.update(role="non_billing_image" if image else "transport_support", basis="human",
                       status="non_billing" if image else "supporting",
                       label="已确认·非账单图片" if image else "已确认·支持材料",
                       reading="已人工核对原件", note=latest["reason"])
        elif latest and latest["classification"] == "reopen":
            use.update(basis="human", note=latest["reason"])
        elif evidence:
            use.update(role="transport_support", status="supporting", label="已读取·运输支持材料",
                       basis="native_text", reading="原生文字已完整读取",
                       note="原文明确为提单／运输材料，费用描述未给出明确金额；本次未提取额外费用。",
                       evidence=evidence)
        elif file.get("detected_format") == "eml":
            use.update(role="email", status="email_read", label="邮件已读取", reading="正文已读取",
                       note="邮件正文与附件分别处理。", can_confirm=False)
        result.append({**file, "assessment": use})
    # Parent mail statuses summarize descendants without counting the same issue twice.
    for file in reversed(result):
        if file.get("detected_format") != "eml" or file["assessment"]["status"] in {"error", "fee_pending"}:
            continue
        children = [f["assessment"] for f in result if f.get("parent_source_id") == file["source_id"]]
        unresolved = [c for c in children if c["status"] in {"error", "purpose_pending", "attachments_pending", "fee_pending"}]
        if unresolved:
            file["assessment"].update(status="attachments_pending",
                                      label="附件用途待确认" if all(c["status"] == "purpose_pending" for c in unresolved) else "附件仍需处理",
                                      note="正文费用按自身证据处理；请查看下方仍需处理的附件。")
    return result
