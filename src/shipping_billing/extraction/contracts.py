"""Extraction drafts and evidence-checked candidates; no posting decisions."""

from typing import Literal

from pydantic import Field

from shipping_billing.models import Candidate, Contract, Evidence, Issue, Locator, Number, Text


class Citation(Contract):
    unit_id: Text
    quote: Text


class FeeDraft(Contract):
    charge_item_raw: Text | None
    amount: Number | None
    currency_raw: Text | None
    quantity: Number | None
    unit_price: Number | None
    payment_term_raw: Text | None
    evidence: dict[str, list[Citation]]


class DocumentDraft(Contract):
    document_type: Text | None
    reference_no: Text | None
    document_date_raw: Text | None
    issuer: Text | None
    recipient: Text | None
    shipping_info: dict[str, Text | list[Text]]
    evidence: dict[str, list[Citation]]
    fees: list[FeeDraft]


class Observation(Contract):
    kind: Literal["total", "paid_amount", "general_term", "supporting_information"]
    citations: list[Citation] = Field(min_length=1)


class ExtractionDraft(Contract):
    documents: list[DocumentDraft]
    observations: list[Observation]


class EvidenceUnit(Contract):
    unit_id: Text
    source_id: Text
    text: Text
    locator: Locator
    method: Text
    metadata: dict = Field(default_factory=dict)


class ExtractedCandidate(Candidate):
    source_id: Text
    shipping_info: dict[str, str | list[str]] = Field(default_factory=dict)
    field_evidence: dict[str, list[Evidence]] = Field(default_factory=dict)


class ExtractionFile(Contract):
    source_id: Text
    parent_source_id: str | None = None
    status: Literal["extracted", "no_charge", "pending_review", "error"]
    candidates: list[ExtractedCandidate] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    attempts: list[dict] = Field(default_factory=list)


class ExtractionReport(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    mode: Literal["live", "replay", "test_injected"]
    prompt_version: Text
    rules_version: Literal["stage4-evidence-checks-v1"] = "stage4-evidence-checks-v1"
    input_sha256: Text
    files: list[ExtractionFile] = Field(default_factory=list)
    evidence_units: list[EvidenceUnit] = Field(default_factory=list)
    model_invocations: int = 0
    billing_pipeline_executed: Literal[False] = False
