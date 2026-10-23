"""Versioned public contracts; processing rules live in dedicated modules."""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    WithJsonSchema,
    model_validator,
)


def numeric(value):
    if type(value) not in (int, float, Decimal):
        raise ValueError("amount must be a JSON number, not a string or boolean")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("amount must be finite")
    return result


Number = Annotated[Decimal, BeforeValidator(numeric), WithJsonSchema({"type": "number"})]
Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Operation = Literal["CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Locator(Contract):
    kind: Literal[
        "email_body",
        "email_header",
        "pdf_page",
        "image_region",
        "sheet_cells",
        "word_block",
        "existing_record",
    ]
    page: Annotated[int, Field(strict=True, ge=1)] | None = None
    section: Text | None = None
    region: Text | None = None
    sheet: Text | None = None
    cells: Text | None = None
    block: Text | None = None
    pointer: Text | None = None

    @model_validator(mode="after")
    def location_is_concrete(self):
        required = {
            "pdf_page": ("page",),
            "image_region": ("region",),
            "sheet_cells": ("sheet", "cells"),
            "word_block": ("block",),
            "email_body": ("section",),
            "email_header": ("section",),
            "existing_record": ("pointer",),
        }[self.kind]
        if any(getattr(self, key) is None for key in required):
            raise ValueError("locator requires a concrete position for its kind")
        return self


class Evidence(Contract):
    source_id: Text
    locator: Locator
    quote: Text


class Issue(Contract):
    code: Text
    severity: Literal["info", "warning", "error"]
    blocks_automation: StrictBool
    field: str | None = None
    message: Text


def same_json_value(a, b):
    """JSON booleans are distinct from numbers, including inside custom fields."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same_json_value(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same_json_value(x, y) for x, y in zip(a, b))
    return a == b


class Change(Contract):
    path: Annotated[
        str,
        Field(
            pattern=r"^/(document_info|transaction_party|charge_details|financial_attributes|shipping_info|extensions)/.+"
        ),
    ]
    old: Any
    new: Any
    evidence: Annotated[list[Evidence], Field(min_length=1)]

    @model_validator(mode="after")
    def is_change(self):
        if same_json_value(self.old, self.new):
            raise ValueError("a change must have different values")
        return self


class AuditTrail(Contract):
    ai_reasoning: Annotated[list[Text], Field(min_length=1)]
    sources: list[Text] = Field(default_factory=list)
    field_evidence: dict[str, list[Evidence]] = Field(default_factory=dict)
    issues: list[Issue] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        evidence = [e for values in self.field_evidence.values() for e in values]
        evidence += [e for c in self.changes for e in c.evidence]
        if any(e.source_id not in self.sources for e in evidence):
            raise ValueError("evidence source must be registered in audit sources")
        if any(not key.startswith("/") for key in self.field_evidence):
            raise ValueError("field evidence keys must be JSON pointers")
        if len({c.path for c in self.changes}) != len(self.changes):
            raise ValueError("duplicate change paths")
        return self


class DocumentInfo(Contract):
    document_type: Text | None
    reference_no: Text | None
    document_date: str | None
    document_date_raw: str | None = None

    @model_validator(mode="after")
    def iso_date(self):
        if self.document_date is not None:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.document_date):
                raise ValueError("date must use YYYY-MM-DD")
            date.fromisoformat(self.document_date)
        return self


class TransactionParty(Contract):
    issuer: Text | None
    recipient: Text | None


class ChargeDetails(Contract):
    charge_item: Text | None
    amount: Number | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None
    payment_term: Literal["PP", "CC"] | None
    charge_item_raw: str | None = None
    quantity: Number | None = None
    unit_price: Number | None = None
    payment_term_raw: str | None = None
    currency_raw: str | None = None


class FinancialAttributes(Contract):
    accounting_entity: Text | None
    posting_type: Literal["AR", "AP"] | None


class SnapshotDocumentInfo(DocumentInfo):
    model_config = ConfigDict(extra="allow")


class SnapshotTransactionParty(TransactionParty):
    model_config = ConfigDict(extra="allow")


class SnapshotChargeDetails(ChargeDetails):
    model_config = ConfigDict(extra="allow")


class SnapshotFinancialAttributes(FinancialAttributes):
    model_config = ConfigDict(extra="allow")


class BillSnapshot(BaseModel):
    """Old snapshots preserve unknown root fields; do not reclassify historical operations."""

    model_config = ConfigDict(extra="allow")
    record_id: Text
    document_info: SnapshotDocumentInfo
    transaction_party: SnapshotTransactionParty
    charge_details: SnapshotChargeDetails
    financial_attributes: SnapshotFinancialAttributes
    extensions: dict[str, Any] = Field(default_factory=dict)


class Candidate(Contract):
    source_line_id: Text
    document_info: DocumentInfo
    transaction_party: TransactionParty
    charge_details: ChargeDetails
    evidence: list[Evidence]
    issues: list[Issue] = Field(default_factory=list)


class BillingRecord(BillSnapshot):
    operation: Operation
    existing_record_id: Text | None = None
    audit_trail: AuditTrail
    shipping_info: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def decision_is_well_formed(self):
        blockers = any(i.blocks_automation for i in self.audit_trail.issues)
        if self.operation in ("CREATE", "UPDATE", "NO_CHANGE"):
            if (
                self.charge_details.amount is None
                or self.charge_details.currency is None
                or self.charge_details.charge_item is None
                or self.financial_attributes.accounting_entity is None
                or self.financial_attributes.posting_type is None
                or blockers
            ):
                raise ValueError("automatic decisions require core facts and no blocking issue")
        if self.operation == "PENDING_REVIEW" and not blockers:
            raise ValueError("pending review requires an explicit blocking issue")
        if self.operation in ("UPDATE", "NO_CHANGE"):
            if self.existing_record_id != self.record_id:
                raise ValueError("matched decisions must retain the old record identity")
        if self.operation == "UPDATE" and not self.audit_trail.changes:
            raise ValueError("update requires a nonempty evidence-backed change plan")
        if self.operation != "UPDATE" and self.audit_trail.changes:
            raise ValueError("only UPDATE may carry applied changes")
        if self.operation == "CREATE" and self.existing_record_id is not None:
            raise ValueError("create must not target an existing record")
        return self


class SourceDocument(Contract):
    source_id: Text
    path: Text | None = None
    url: Text | None = None
    role: Literal["business_document", "supporting_document"] = "business_document"

    @model_validator(mode="after")
    def one_location(self):
        if (self.path is None) == (self.url is None):
            raise ValueError("provide exactly one path or URL")
        if self.url:
            parsed = urlsplit(self.url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("URL must be HTTP(S) without embedded credentials")
        return self


class BillingRequest(Contract):
    schema_version: Literal["0.1.0"]
    documents: Annotated[list[SourceDocument], Field(min_length=1)]
    accounting_entity: Text | None
    existing_bills: list[BillSnapshot] = Field(default_factory=list)
    party_aliases: dict[Text, Text] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_identities(self):
        for ids in ([d.source_id for d in self.documents], [r.record_id for r in self.existing_bills]):
            if len(set(ids)) != len(ids):
                raise ValueError("source and old record identities must be unique")
        return self


class DocumentFragment(Contract):
    fragment_id: Text
    source_id: Text
    text: str
    locator: Locator


class FileResult(Contract):
    source_id: Text
    parent_source_id: Text | None = None
    status: Literal["processed", "no_charge", "pending_review", "error"]
    issues: list[Issue] = Field(default_factory=list)


class RunManifest(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    mode: Literal["offline", "online", "replay"]
    files: list[FileResult]
