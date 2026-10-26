"""Raw documents to validated billing decisions, with source-complete partial failure."""

import hashlib
import re
from pathlib import Path
from uuid import uuid4

from shipping_billing.domain.contracts import RULES_VERSION
from shipping_billing.domain.engine import normalize, problem
from shipping_billing.errors import BillingError
from shipping_billing.evaluation import RECORDS
from shipping_billing.extraction.client import StructuredModelClient
from shipping_billing.extraction.engine import ExtractionEngine, digest
from shipping_billing.extraction.prompt import PROMPT_VERSION as EXTRACTION_VERSION
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.ocr.client import ADAPTER_VERSION
from shipping_billing.ocr.tunnel import PageOCR
from shipping_billing.parsers.engine import DocumentParser
from shipping_billing.pipeline.contracts import VERSION, AuditEntry, PipelineFile, PipelineManifest
from shipping_billing.pipeline.inputs import discover
from shipping_billing.pipeline.intents import PROMPT_VERSION as INTENT_VERSION
from shipping_billing.pipeline.intents import IntentEngine, merge_directives
from shipping_billing.pipeline.source_status import PURPOSE_REQUIRED, empty_image_requires_confirmation
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext
from shipping_billing.reconciliation.engine import reconcile

ARTIFACTS = (
    "records.json",
    "manifest.json",
    "audit.json",
    "state.json",
    "parse.json",
    "extraction.json",
    "normalized.json",
    "reconciliation.json",
    "intents.json",
    "context.json",
    "ocr-replay-map.json",
)
VERSIONS = {
    "workflow": VERSION,
    "parser": "stage3-parser-v1",
    "ocr": ADAPTER_VERSION,
    "extraction": EXTRACTION_VERSION,
    "normalization": RULES_VERSION,
    "reconciliation": "stage6-reconcile-v1",
    "intents": INTENT_VERSION,
}


def cache_profile(storage, settings, mode, selected=None):
    if mode in ("live", "test_injected"):
        profile = {
            "provider": settings.model_provider,
            "model": settings.model_name,
            "base_url": settings.model_base_url,
            "origin": mode,
            "versions": VERSIONS,
        }
        key = digest(profile)
        # Stable profiles need no replacement; Windows readers can temporarily prevent replacing open files.
        for path, value in (
            (storage / "profiles" / (key + ".json"), profile),
            (storage / "active-profile.json", {"namespace": key}),
        ):
            if not path.is_file() or load_json(path) != value:
                write_json(path, value)
    else:
        if selected is None and (storage / "active-profile.json").is_file():
            active = load_json(storage / "active-profile.json")
            if not isinstance(active, dict) or "namespace" not in active:
                raise BillingError("INVALID_CACHE_PROFILE", "Active cache index must name a namespace.")
            selected = active["namespace"]
        if selected is None:
            profile = {"provider": None, "model": None, "origin": "unconfigured", "versions": VERSIONS}
            return digest(profile), profile
        if not isinstance(selected, str) or not re.fullmatch("[a-f0-9]{64}", selected):
            raise BillingError("INVALID_CACHE_PROFILE", "Cache profile must be a SHA-256 namespace.")
        profile = load_json(storage / "profiles" / (selected + ".json"))
        if (
            not isinstance(profile, dict)
            or digest(profile) != selected
            or profile.get("versions") != VERSIONS
        ):
            raise BillingError("CACHE_PROFILE_MISMATCH", "Cache profile or pipeline rule versions changed.")
        key = selected
    return key, profile


def preflight(output, inputs, protected):
    if output.exists() and not output.is_dir():
        raise BillingError("INVALID_OUTPUT_DIRECTORY", "Workflow output must be a directory.")
    targets = [output / name for name in ARTIFACTS]
    forbidden = {p.resolve() for p in [*inputs, *protected]}
    if any(p.resolve() in forbidden for p in targets):
        raise BillingError(
            "OUTPUT_COLLIDES_WITH_INPUT", "Workflow output would overwrite an input or configuration."
        )
    if any(p.exists() for p in targets):
        raise BillingError(
            "OUTPUT_ALREADY_EXISTS", "Use a new output directory to preserve earlier run artifacts."
        )


def run_pipeline(
    inputs,
    *,
    settings,
    output,
    storage,
    mode="replay",
    state=None,
    context=None,
    source_id=None,
    ocr_map=None,
    cloudflared=None,
    local_ocr=None,
    cache_namespace=None,
    protected=(),
    client=None,
    ocr_provider=None,
    doc_converter=None,
    progress=None,
):
    if mode not in ("live", "replay", "offline", "test_injected"):
        raise BillingError("INVALID_RUN_MODE", "Unknown workflow mode.")
    state = state or LedgerState()
    context = (context or ReconciliationContext()).model_copy(deep=True)
    output, storage = Path(output).resolve(), Path(storage).resolve()
    paths, discovery = discover([Path(p) for p in inputs], excluded=[output, storage], source_id=source_id)
    preflight(output, [p for _, p in paths], [Path(p) for p in protected])
    if mode in ("live", "test_injected") and any(
        p.resolve().is_relative_to(storage) for p in [*[p for _, p in paths], *[Path(p) for p in protected]]
    ):
        raise BillingError(
            "INPUT_STORAGE_OVERLAP", "Writable cache storage cannot contain input documents or configuration."
        )
    if mode == "live" and settings.mode != "online":
        raise BillingError("MODEL_NOT_CONFIGURED", "Live run requires explicit online model configuration.")
    namespace, profile = cache_profile(storage, settings, mode, cache_namespace)
    model_root = storage / "models" / namespace
    if progress:
        progress("parse", len(paths))
    provider = None
    ocr_replay = {}
    parse_mode = "offline" if mode == "test_injected" else mode

    def recognize(content, sid, page, content_type):
        nonlocal provider
        if ocr_provider is not None:
            result = ocr_provider(content, sid, page, content_type)
        else:
            if provider is None:
                provider = PageOCR(
                    settings,
                    storage=storage / "parsing" if mode == "live" else output / "work",
                    mode=mode,
                    mapping=ocr_map,
                    cloudflared=cloudflared,
                )
            result = provider(content, sid, page, content_type)
        if result.get("archive") and result.get("input_sha256"):
            ocr_replay[f"{sid}:page-{page}"] = {
                "sha256": result["input_sha256"],
                "archive": result["archive"],
            }
        return result

    parser = DocumentParser(
        storage=output / "work",
        mode=parse_mode,
        ocr_provider=recognize if mode != "offline" or ocr_provider else None,
        local_ocr=local_ocr,
        doc_converter=doc_converter,
    )
    try:
        for sid, path in paths:
            parser.parse_file(path, sid)
    finally:
        if provider:
            ocr_replay.update(provider.replay_map)
            provider.close()
    parsed = parser.report
    if progress:
        progress("extract", len(parsed.sources))
    client = client or StructuredModelClient(settings)
    extraction = ExtractionEngine(
        client, storage=model_root / "extraction", mode="replay" if mode == "offline" else mode
    ).extract(parsed)
    # Every discovered source must survive each transition, even if it has no fee.
    if [s.source_id for s in parsed.sources] != [f.source_id for f in extraction.files]:
        raise BillingError("INCOMPLETE_SOURCE_REGISTRY", "Extraction omitted or reordered an input source.")
    normalized = normalize(extraction, context.normalization_context)
    baseline = reconcile(normalized, extraction, state=state, context=context, parsed=parsed)
    if progress:
        progress("intents", len(normalized.lines))
    intents = IntentEngine(client, storage=model_root / "intents", mode=mode).extract(
        normalized, extraction, state, baseline
    )
    context.lines, conflicts = merge_directives(context.lines, intents.directives)
    line_source = {line.source_line_id: line.source_id for line in normalized.lines}
    intent_files = {f.source_id: f for f in intents.files}
    for file in extraction.files:
        info = intent_files[file.source_id]
        for code in info.issue_codes:
            file.issues.append(problem(code))
        if info.status == "error":
            file.status = "error"
        elif info.status == "pending_review" and file.status != "error":
            file.status = "pending_review"
        if any(line_source[key] == file.source_id for key in conflicts):
            file.issues.append(problem("MANUAL_AUTO_INTENT_CONFLICT"))
            if file.status != "error":
                file.status = "pending_review"
    normalized = normalize(extraction, context.normalization_context)
    if progress:
        progress("reconcile", len(normalized.lines))
    reconciled = reconcile(normalized, extraction, state=state, context=context, parsed=parsed)
    records = [r.model_dump(mode="python", exclude_unset=True) for r in reconciled.records]
    RECORDS.validate_python(records)
    final_files = {f.source_id: f for f in reconciled.files}
    extracted = {f.source_id: f for f in extraction.files}
    files = []
    for source in parsed.sources:
        final = final_files[source.source_id]
        issues = list(final.issues)
        decisions = [d for d in reconciled.decisions if d.source_id == source.source_id]
        for d in decisions:
            for issue in d.issues:
                if issue not in issues:
                    issues.append(issue)
        # Empty image content needs a purpose decision; actual execution failures remain errors.
        ocr_failed = any(p["status"] == "error" for p in source.metadata.get("ocr_page_status", []))
        status = "error" if ocr_failed else final.status
        if empty_image_requires_confirmation(
                    source.model_dump(mode="python"),
                    has_fees=bool(decisions or extracted[source.source_id].candidates),
                    issue_codes={i.code for i in issues if i.blocks_automation}):
            status = "pending_review"
            issues.append(problem(PURPOSE_REQUIRED))
        files.append(
            PipelineFile(
                source_id=source.source_id,
                parent_source_id=source.parent_source_id,
                original_name=source.original_name,
                sha256=source.sha256,
                detected_format=source.detected_format,
                status=status,
                issues=issues,
                record_ids=sorted({d.record_id for d in decisions}),
                stages={
                    "parse": source.status,
                    "extraction": extracted[source.source_id].status,
                    "intents": intent_files[source.source_id].status,
                    "reconciliation": final.status,
                },
            )
        )
    # Parent status reflects incomplete descendants without blocking unrelated roots.
    by_id = {f.source_id: f for f in files}
    for file in reversed(files):
        if file.parent_source_id and file.status in ("error", "pending_review"):
            parent = by_id[file.parent_source_id]
            if parent.status != "error":
                parent.status = "pending_review"
            if not any(i.code == "CHILD_REQUIRES_REVIEW" for i in parent.issues):
                parent.issues.append(problem("CHILD_REQUIRES_REVIEW", blocking=False))
    audit = []
    for record in reconciled.records:
        members = [d for d in reconciled.decisions if d.record_id == record.record_id]
        line_ids = {d.source_line_id for d in members}
        rules = {d.match_rule or "unmatched" for d in members}
        rules.update(
            event.rule
            for line in normalized.lines
            if line.source_line_id in line_ids
            for event in line.events
        )
        audit.append(
            AuditEntry(
                record_id=record.record_id,
                operation=record.operation,
                source_line_ids=[d.source_line_id for d in members],
                rules=sorted(rules),
                evidence=record.audit_trail.field_evidence,
                changes=[c.model_dump(mode="python") for c in record.audit_trail.changes],
                issue_codes=sorted({i.code for d in members for i in d.issues}),
            ).model_dump(mode="python")
        )
    artifacts = {
        "records.json": records,
        "state.json": reconciled.next_state.model_dump(mode="python"),
        "parse.json": parsed.model_dump(mode="python"),
        "extraction.json": extraction.model_dump(mode="python"),
        "normalized.json": normalized.model_dump(mode="python"),
        "reconciliation.json": reconciled.model_dump(mode="python"),
        "intents.json": intents.model_dump(mode="python"),
        "audit.json": audit,
        "context.json": context.model_dump(mode="python"),
        "ocr-replay-map.json": ocr_replay,
    }
    error = any(f.status == "error" for f in files)
    pending = any(f.status == "pending_review" for f in files)
    counts = {
        op: sum(d.operation == op for d in reconciled.decisions)
        for op in ("CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW")
    }
    counts.update(
        input_documents=len(paths),
        sources=len(files),
        fee_lines=len(reconciled.decisions),
        records=len(records),
        ignored_inputs=sum(d.disposition == "ignored" for d in discovery),
        purpose_pending_sources=sum(any(i.code == PURPOSE_REQUIRED for i in f.issues) for f in files),
    )
    manifest = PipelineManifest(
        run_id=uuid4().hex,
        mode=mode,
        status="error" if error else "pending_review" if pending else "completed",
        discovery=discovery,
        files=files,
        counts=counts,
        model_invocations=extraction.model_invocations + intents.model_invocations,
        ocr_network_calls=0
        if mode in ("replay", "offline", "test_injected")
        else provider.client.network_calls
        if provider is not None
        else None
        if any(p["status"] == "error" for s in parsed.sources for p in s.metadata.get("ocr_page_status", []))
        else sum(r.get("network_calls", 0) for s in parsed.sources for r in s.ocr_results),
        cache_profile={"namespace": namespace, **profile},
        versions=VERSIONS,
        input_state_sha256=digest(state.model_dump(mode="python")),
        context_sha256=digest(context.model_dump(mode="python")),
        artifact_sha256={
            name: hashlib.sha256((dumps(value) + "\n").encode("utf-8")).hexdigest()
            for name, value in artifacts.items()
        },
        exit_code=3 if error else 1 if pending else 0,
    )
    # Validate every artifact before publishing. Manifest is written last as the completion marker.
    for value in artifacts.values():
        dumps(value)
    if progress:
        progress("write", len(records))
    for name, value in artifacts.items():
        write_json(output / name, value)
    write_json(output / "manifest.json", manifest.model_dump(mode="python"))
    return manifest
