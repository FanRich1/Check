"""Stage 6 input, intent, persisted identity and decision contracts."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_serializer, model_validator

from shipping_billing.domain.contracts import NormalizationContext
from shipping_billing.models import (
    BillingRecord,
    BillSnapshot,
    Change,
    Contract,
    Evidence,
    Issue,
    Operation,
    Text,
)


class EditIntent(Contract):
    path: Annotated[
        str, Field(pattern=r"^/(document_info|transaction_party|charge_details|shipping_info|extensions)/.+")
    ]
    action: Literal["set", "clear"]
    expected_old: Any
    value: Any
    evidence: Annotated[list[Evidence], Field(min_length=1)]

    @model_validator(mode="after")
    def explicit_null(self):
        if (self.action == "clear") != (self.value is None):
            raise ValueError("clear requires null; setting null must explicitly use clear")
        return self


class RecordSelector(Contract):
    record_id: Text
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class LineDirective(Contract):
    selector: RecordSelector | None = None
    edits: list[EditIntent] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_edit_paths(self):
        paths = [e.path for e in self.edits]
        if len(paths) != len(set(paths)):
            raise ValueError("an intent may authorize each path once")
        return self


class ReconciliationContext(Contract):
    normalization_context: NormalizationContext = Field(default_factory=NormalizationContext)
    lines: dict[Text, LineDirective] = Field(default_factory=dict)


class IdentityEntry(Contract):
    record_id: Text
    occurrence_keys: list[Text] = Field(default_factory=list)


class LedgerState(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    bills: list[BillSnapshot] = Field(default_factory=list)
    identities: list[IdentityEntry] = Field(default_factory=list)
    audit_log: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_identities(self):
        ids = [b.record_id for b in self.bills]
        if len(ids) != len(set(ids)):
            raise ValueError("existing record IDs must be unique")
        indices = [e.record_id for e in self.identities]
        if len(indices) != len(set(indices)) or set(indices) - set(ids):
            raise ValueError("identity entries must reference unique known bills")
        keys = [k for e in self.identities for k in e.occurrence_keys]
        if len(keys) != len(set(keys)):
            raise ValueError("one physical occurrence cannot identify multiple bills")
        return self

    @model_serializer
    def preserve_snapshot_shape(self):
        return {
            "schema_version": self.schema_version,
            "bills": [b.model_dump(mode="python", exclude_unset=True) for b in self.bills],
            "identities": [e.model_dump(mode="python") for e in self.identities],
            "audit_log": self.audit_log,
        }


class LineDecision(Contract):
    source_line_id: Text
    source_id: Text
    operation: Operation
    record_id: Text
    matched_record_ids: list[str] = Field(default_factory=list)
    match_rule: str | None = None
    duplicate_of: str | None = None
    issues: list[Issue] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    proposed_changes: list[Change] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict)


class DecisionFile(Contract):
    source_id: Text
    parent_source_id: str | None = None
    status: Literal["processed", "no_charge", "pending_review", "error"]
    issues: list[Issue] = Field(default_factory=list)


class ReconciliationReport(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    rules_version: Literal["stage6-reconcile-v1"] = "stage6-reconcile-v1"
    input_sha256: Text
    previous_state_sha256: Text
    records: list[BillingRecord]
    decisions: list[LineDecision]
    files: list[DecisionFile]
    next_state: LedgerState
    model_invocations: Literal[0] = 0
    billing_pipeline_executed: Literal[False] = False
