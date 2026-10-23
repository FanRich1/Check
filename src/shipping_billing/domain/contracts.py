"""Stage 5 facts, policy and results; matching and ledger operations are later stages."""

from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, field_validator, model_validator

from shipping_billing.extraction.contracts import ExtractedCandidate
from shipping_billing.models import Contract, Evidence, FinancialAttributes, Issue, Number, Text

RULES_VERSION = "stage5-domain-v1"
Rounding = Literal["exact", "HALF_UP", "HALF_EVEN"]


class SupportedFact(Contract):
    value: Number
    evidence: Annotated[list[Evidence], Field(min_length=1)]

    @field_validator("value")
    @classmethod
    def bounded_number(cls, value):
        if len(value.as_tuple().digits) > 128 or abs(value.as_tuple().exponent) > 128:
            raise ValueError("monetary facts exceed the supported numeric limit")
        return value


class SettlementRoles(Contract):
    payer: Text
    payee: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class Adjustment(Contract):
    kind: Literal["credit", "reversal", "discount"]
    original_reference: Text
    original_payer: Text
    original_payee: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class IdentifierCorrection(Contract):
    path: Annotated[
        str,
        Field(
            pattern=r"^/(document_info/reference_no|shipping_info/(mbl_no|hbl_no|booking_no|container_nos/\d+))$"
        ),
    ]
    original_value: Text
    corrected_value: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class LinePolicy(Contract):
    settlement: SettlementRoles | None = None
    adjustment: Adjustment | None = None
    rounding: Rounding | None = None
    payment_scope: Literal["line", "document", "freight", "unknown"] = "unknown"
    tax: SupportedFact | None = None
    discount: SupportedFact | None = None
    corrections: list[IdentifierCorrection] = Field(default_factory=list)


class DocumentTotals(Contract):
    group_id: Text
    source_id: Text
    line_ids: Annotated[list[Text], Field(min_length=1)]
    currency: Text
    currency_evidence: Annotated[list[Evidence], Field(min_length=1)]
    total: SupportedFact | None = None
    subtotal: SupportedFact | None = None
    tax: SupportedFact | None = None
    discount: SupportedFact | None = None
    paid: SupportedFact | None = None
    balance: SupportedFact | None = None

    @model_validator(mode="after")
    def unique_rows(self):
        if len(set(self.line_ids)) != len(self.line_ids):
            raise ValueError("total scope must not repeat a fee row")
        if not any(getattr(self, k) for k in ("total", "subtotal", "paid", "balance")):
            raise ValueError("total scope needs a checkable fact")
        return self


class NormalizationContext(Contract):
    accounting_entity: Text | None = None
    party_aliases: dict[Text, Text] = Field(default_factory=dict)
    charge_aliases: dict[Text, Text] = Field(default_factory=dict)
    rounding: Rounding = "exact"
    lines: dict[Text, LinePolicy] = Field(default_factory=dict)
    totals: list[DocumentTotals] = Field(default_factory=list)
    use_observed_totals: StrictBool = True


class RuleEvent(Contract):
    rule: Text
    path: Text
    old: Any
    new: Any
    evidence: list[Evidence] = Field(default_factory=list)
    context_pointer: str | None = None


class Check(Contract):
    code: Text
    status: Literal["passed", "not_checkable", "failed"]
    expected: Number | None = None
    actual: Number | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    message: Text


class NormalizedLine(Contract):
    source_line_id: Text
    source_id: Text
    original: ExtractedCandidate
    candidate: ExtractedCandidate
    financial_attributes: FinancialAttributes
    status: Literal["ready", "pending_review"] = "pending_review"
    balance_effect: Literal["increase", "decrease", "none", "unknown"] = "unknown"
    currency_minor_units: int | None = None
    settlement_condition: str | None = None
    issues: list[Issue] = Field(default_factory=list)
    events: list[RuleEvent] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)


class NormalizedFile(Contract):
    source_id: Text
    parent_source_id: str | None = None
    status: Literal["ready", "no_charge", "pending_review", "error"]
    line_ids: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class NormalizationReport(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    rules_version: Literal["stage5-domain-v1"] = RULES_VERSION
    currency_snapshot: dict
    input_sha256: Text
    context_sha256: Text
    files: list[NormalizedFile]
    lines: list[NormalizedLine]
    document_checks: list[Check] = Field(default_factory=list)
    model_invocations: Literal[0] = 0
    billing_pipeline_executed: Literal[False] = False
