"""Final workflow manifests; intermediate stage reports keep their original contracts."""

from typing import Any, Literal

from pydantic import Field

from shipping_billing.models import Contract, Evidence, FileResult, Text
from shipping_billing.reconciliation.contracts import LineDirective

VERSION = "stage7-workflow-v1"


class DiscoveredInput(Contract):
    path: str
    source_id: str | None = None
    disposition: Literal["selected", "ignored"]
    reason: str | None = None


class PipelineFile(FileResult):
    original_name: str
    sha256: str | None
    detected_format: str
    stages: dict[str, str]
    record_ids: list[str] = Field(default_factory=list)


class IntentFile(Contract):
    source_id: Text
    status: Literal["not_needed", "completed", "pending_review", "error"]
    issue_codes: list[str] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class IntentReport(Contract):
    version: Literal["stage7-intents-v1"] = "stage7-intents-v1"
    directives: dict[str, LineDirective] = Field(default_factory=dict)
    files: list[IntentFile] = Field(default_factory=list)
    model_invocations: int = 0


class AuditEntry(Contract):
    record_id: Text
    operation: str
    source_line_ids: list[str]
    rules: list[str]
    evidence: dict[str, list[Evidence]]
    changes: list[dict[str, Any]]
    issue_codes: list[str]


class PipelineManifest(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    workflow_version: Literal["stage7-workflow-v1"] = VERSION
    run_id: Text
    mode: Literal["live", "replay", "offline", "test_injected"]
    status: Literal["completed", "pending_review", "error"]
    billing_pipeline_executed: Literal[True] = True
    discovery: list[DiscoveredInput]
    files: list[PipelineFile]
    counts: dict[str, int]
    model_invocations: int
    ocr_network_calls: int | None
    cache_profile: dict[str, Any]
    versions: dict[str, str]
    input_state_sha256: Text
    context_sha256: Text
    artifact_sha256: dict[str, str]
    exit_code: Literal[0, 1, 3]
