"""Explicit parsing, OCR, contract validation and evaluation commands."""

import argparse
import logging
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from shipping_billing.config import Settings
from shipping_billing.domain.contracts import NormalizationContext, NormalizationReport
from shipping_billing.errors import BillingError
from shipping_billing.evaluation import RECORDS, evaluate
from shipping_billing.extraction.contracts import ExtractionReport
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.models import BillingRequest, Candidate, RunManifest
from shipping_billing.parsers.contracts import ParseReport
from shipping_billing.pipeline.contracts import IntentReport, PipelineManifest
from shipping_billing.reconciliation.contracts import LedgerState, ReconciliationContext, ReconciliationReport

SCHEMAS = {
    "request": TypeAdapter(BillingRequest),
    "records": RECORDS,
    "candidate": TypeAdapter(Candidate),
    "manifest": TypeAdapter(RunManifest),
    "parse-report": TypeAdapter(ParseReport),
    "extraction-report": TypeAdapter(ExtractionReport),
    "normalization-context": TypeAdapter(NormalizationContext),
    "normalization-report": TypeAdapter(NormalizationReport),
    "ledger-state": TypeAdapter(LedgerState),
    "reconciliation-context": TypeAdapter(ReconciliationContext),
    "reconciliation-report": TypeAdapter(ReconciliationReport),
    "pipeline-manifest": TypeAdapter(PipelineManifest),
    "intent-report": TypeAdapter(IntentReport),
}
LOG = logging.getLogger("shipping_billing")


def parser():
    p = argparse.ArgumentParser(
        prog="shipping-billing", description="Shipping billing parsing and validation tooling"
    )
    p.add_argument("--version", action="version", version="shipping-billing 0.1.0")
    p.add_argument("--env-file", type=Path, help="Explicit optional dotenv file; never auto-load cwd secrets")
    sub = p.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="Validate a request or a final record array")
    val.add_argument("kind", choices=["request", "records"])
    val.add_argument("input", type=Path)
    val.add_argument("--output", type=Path, help="Optional normalized JSON file")
    schema = sub.add_parser(
        "schema", help="Export structural JSON Schema; Python validators enforce semantic rules"
    )
    schema.add_argument("kind", choices=SCHEMAS)
    schema.add_argument("--output", type=Path)
    config = sub.add_parser("config-check", help="Check configuration without any network calls")
    config.add_argument("--mode", choices=["offline", "online"])
    norm = sub.add_parser("normalize", help="Apply deterministic normalization, money and AR/AP rules")
    norm.add_argument("input", type=Path, help="Stage 4 extraction report")
    norm.add_argument(
        "--context", type=Path, help="Explicit accounting entity, aliases and evidence-backed policy"
    )
    norm.add_argument("--accounting-entity", help="Explicit accounting entity; do not combine with context")
    norm.add_argument("--output", type=Path, required=True)
    rec = sub.add_parser("reconcile", help="Match normalized fees and preview authorized incremental changes")
    rec.add_argument("input", type=Path, help="Stage 5 normalization report")
    rec.add_argument("--extraction", type=Path, required=True, help="Exact stage 4 evidence report")
    rec.add_argument(
        "--context", type=Path, help="Normalization context plus evidence-backed edit directives"
    )
    old = rec.add_mutually_exclusive_group()
    old.add_argument("--state", type=Path, help="Persisted ledger state, including deduplication identities")
    old.add_argument("--existing-bills", type=Path, help="Initial old bill JSON array")
    rec.add_argument(
        "--parse-report", type=Path, help="Optional matching parse report for source byte hashes"
    )
    rec.add_argument("--output", type=Path, required=True)
    rec.add_argument("--records-output", type=Path)
    rec.add_argument("--state-output", type=Path)
    extract = sub.add_parser("extract", help="Extract evidence-checked fee candidates from a parse report")
    extract.add_argument("input", type=Path)
    extract.add_argument("--mode", choices=["live", "replay"], required=True)
    extract.add_argument("--storage", type=Path, default=Path("data/local/extraction"))
    extract.add_argument("--output", type=Path, required=True)
    parse = sub.add_parser("parse", help="Extract source evidence; does not produce accepted billing records")
    parse.add_argument("inputs", type=Path, nargs="+")
    parse.add_argument("--source-id", help="Explicit root source ID, for a single input")
    parse.add_argument("--mode", choices=["offline", "live", "replay"], default="offline")
    parse.add_argument(
        "--ocr-map", type=Path, help="Map page IDs to matching hashes and URLs or replay archives"
    )
    parse.add_argument(
        "--cloudflared", type=Path, help="Explicitly enable temporary file delivery in live mode"
    )
    parse.add_argument("--storage", type=Path, default=Path("data/local/parsing"))
    parse.add_argument("--output", type=Path, required=True)
    parse.add_argument(
        "--local-ocr",
        action="store_true",
        help="Enable supplementary local OCR, explicitly distinct from specified OCR",
    )
    probe = sub.add_parser("probe-model", help="One real LangChain call using synthetic invoice text")
    probe.add_argument("--report", type=Path, required=True)
    ocr = sub.add_parser("ocr", help="Recognize a supplied downloadable URL or explicitly replay an archive")
    location = ocr.add_mutually_exclusive_group(required=True)
    location.add_argument("--file-url")
    location.add_argument("--local-file", type=Path)
    ocr.add_argument("--url-map", type=Path)
    ocr.add_argument("--source-id", required=True)
    ocr.add_argument("--sha256")
    ocr.add_argument("--storage", type=Path, default=Path("data/local/ocr"))
    ocr.add_argument("--replay", type=Path)
    ocr.add_argument("--cache", action="store_true")
    ocr.add_argument("--report", type=Path, required=True)
    ev = sub.add_parser("evaluate", help="Compare separate supplied predictions with one authored case")
    ev.add_argument("--case", type=Path, required=True)
    ev.add_argument("--actual", type=Path, required=True)
    ev.add_argument("--report", type=Path, required=True)
    run = sub.add_parser(
        "run", help="Process documents through parsing, evidence extraction and final billing decisions"
    )
    run.add_argument("inputs", type=Path, nargs="+", help="Files or recursively scanned document directories")
    run.add_argument("--mode", choices=["live", "replay", "offline"], default="replay")
    run.add_argument("--source-id", help="Optional explicit ID for exactly one input")
    run.add_argument("--accounting-entity", help="Explicit accounting entity; do not combine with context")
    run.add_argument("--context", type=Path, help="Normalization policy and optional manual edit directives")
    old_run = run.add_mutually_exclusive_group()
    old_run.add_argument("--state", type=Path)
    old_run.add_argument("--existing-bills", type=Path)
    run.add_argument(
        "--ocr-map", type=Path, help="Page URL map in live mode; exact page archive map in replay"
    )
    run.add_argument("--cloudflared", type=Path, help="Explicit temporary OCR document delivery in live mode")
    run.add_argument("--local-ocr", action="store_true", help="Explicitly enable supplementary local OCR")
    run.add_argument("--storage", type=Path, default=Path("data/local/pipeline"))
    run.add_argument(
        "--cache-profile", help="Select an existing SHA-256 model/rule cache namespace in replay"
    )
    run.add_argument(
        "--output", type=Path, required=True, help="New directory for records, manifest, state and audit"
    )
    return p


def protect_outputs(output, inputs):
    if output and output.resolve() in {p.resolve() for p in inputs}:
        raise BillingError("OUTPUT_COLLIDES_WITH_INPUT", "Output must not overwrite input or label files.")


def main(argv=None):
    # argparse handles --help without loading settings or requiring credentials.
    args = parser().parse_args(argv)
    try:
        if args.env_file is not None and not args.env_file.is_file():
            raise BillingError(
                "ENV_FILE_NOT_FOUND", "The explicitly selected environment file does not exist."
            )
        if args.env_file is not None:
            protect_outputs(getattr(args, "report", None), [args.env_file])
            protect_outputs(getattr(args, "output", None), [args.env_file])
        overrides = {"mode": args.mode} if args.command == "config-check" and args.mode else {}
        settings = Settings(_env_file=args.env_file, **overrides)
        logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s %(message)s")
        if args.command == "config-check":
            print(dumps(settings.public_summary()))
        elif args.command == "reconcile":
            from shipping_billing.reconciliation.engine import reconcile

            inputs = [args.input, args.extraction] + [
                p
                for p in (args.context, args.state, args.existing_bills, args.parse_report, args.env_file)
                if p
            ]
            outputs = [p for p in (args.output, args.records_output, args.state_output) if p]
            for output in outputs:
                protect_outputs(output, inputs)
            if len({p.resolve() for p in outputs}) != len(outputs):
                raise BillingError(
                    "OUTPUT_PATH_COLLISION", "Report, records and state need distinct output paths."
                )
            context = (
                ReconciliationContext.model_validate(load_json(args.context))
                if args.context
                else ReconciliationContext()
            )
            state = LedgerState.model_validate(load_json(args.state)) if args.state else LedgerState()
            if args.existing_bills:
                state = LedgerState(bills=load_json(args.existing_bills))
            result = reconcile(
                NormalizationReport.model_validate(load_json(args.input)),
                ExtractionReport.model_validate(load_json(args.extraction)),
                state=state,
                context=context,
                parsed=ParseReport.model_validate(load_json(args.parse_report))
                if args.parse_report
                else None,
            )
            write_json(args.output, result.model_dump(mode="python"))
            if args.records_output:
                write_json(
                    args.records_output,
                    [r.model_dump(mode="python", exclude_unset=True) for r in result.records],
                )
            if args.state_output:
                write_json(args.state_output, result.next_state.model_dump(mode="python"))
            counts = {
                op: sum(d.operation == op for d in result.decisions)
                for op in ("CREATE", "UPDATE", "NO_CHANGE", "PENDING_REVIEW")
            }
            print(
                dumps(
                    {
                        "lines": len(result.decisions),
                        "records": len(result.records),
                        **counts,
                        "model_invocations": 0,
                        "billing_pipeline_executed": False,
                    }
                )
            )
            return (
                3
                if any(f.status == "error" for f in result.files)
                else 1
                if any(f.status == "pending_review" for f in result.files)
                else 0
            )
        elif args.command == "normalize":
            from shipping_billing.domain.engine import normalize

            protect_outputs(args.output, [args.input] + ([args.context] if args.context else []))
            if args.context and args.accounting_entity:
                raise BillingError(
                    "CONFLICTING_CONTEXT_ARGUMENTS", "Use either context or accounting-entity."
                )
            context = (
                NormalizationContext.model_validate(load_json(args.context))
                if args.context
                else NormalizationContext(accounting_entity=args.accounting_entity)
            )
            result = normalize(ExtractionReport.model_validate(load_json(args.input)), context)
            write_json(args.output, result.model_dump(mode="python"))
            counts = {
                s: sum(f.status == s for f in result.files)
                for s in ("ready", "no_charge", "pending_review", "error")
            }
            print(
                dumps(
                    {
                        "lines": len(result.lines),
                        "ready_lines": sum(line.status == "ready" for line in result.lines),
                        **counts,
                        "model_invocations": 0,
                        "billing_pipeline_executed": False,
                    }
                )
            )
            return 3 if counts["error"] else 1 if counts["pending_review"] else 0
        elif args.command == "extract":
            from shipping_billing.extraction.client import StructuredModelClient
            from shipping_billing.extraction.engine import ExtractionEngine

            protect_outputs(args.output, [args.input])
            parsed = ParseReport.model_validate(load_json(args.input))
            if args.mode == "live" and settings.mode != "online":
                raise BillingError("MODEL_NOT_CONFIGURED", "Live extraction requires online model settings.")
            result = ExtractionEngine(
                StructuredModelClient(settings), storage=args.storage, mode=args.mode
            ).extract(parsed)
            write_json(args.output, result.model_dump(mode="python"))
            counts = {
                s: sum(f.status == s for f in result.files)
                for s in ("extracted", "no_charge", "pending_review", "error")
            }
            print(
                dumps(
                    {
                        "mode": result.mode,
                        "files": len(result.files),
                        "candidates": sum(len(f.candidates) for f in result.files),
                        "model_invocations": result.model_invocations,
                        **counts,
                        "billing_pipeline_executed": False,
                    }
                )
            )
            return 3 if counts["error"] else 1 if counts["pending_review"] else 0
        elif args.command == "parse":
            from shipping_billing.ocr.tunnel import PageOCR
            from shipping_billing.parsers.engine import DocumentParser

            protect_outputs(args.output, args.inputs + ([args.ocr_map] if args.ocr_map else []))
            if args.source_id and len(args.inputs) != 1:
                raise BillingError("INVALID_PARSE_ARGUMENTS", "Explicit source-id requires one input file.")
            provider = (
                None
                if args.mode == "offline"
                else PageOCR(
                    settings,
                    storage=args.storage,
                    mode=args.mode,
                    mapping=args.ocr_map,
                    cloudflared=args.cloudflared,
                )
            )
            local_ocr = None
            if args.local_ocr:
                from shipping_billing.parsers.local_text import LocalTextOCR

                local_ocr = LocalTextOCR()
            engine = DocumentParser(
                storage=args.storage, mode=args.mode, ocr_provider=provider, local_ocr=local_ocr
            )
            try:
                for index, path in enumerate(args.inputs, 1):
                    engine.parse_file(path, args.source_id or f"input-{index}")
            finally:
                if provider is not None:
                    provider.close()
            write_json(args.output, engine.report.model_dump(mode="python"))
            counts = {
                status: sum(s.status == status for s in engine.report.sources)
                for status in ("parsed", "pending_review", "error")
            }
            print(
                dumps(
                    {
                        "mode": args.mode,
                        "sources": len(engine.report.sources),
                        **counts,
                        "billing_pipeline_executed": False,
                    }
                )
            )
            return 3 if counts["error"] else 1 if counts["pending_review"] else 0
        elif args.command == "probe-model":
            from shipping_billing.extraction.client import ProbeCharge, StructuredModelClient

            result, meta = StructuredModelClient(settings).extract(
                "Invoice number INV-PROBE-001. Actual fee amount 40 USD. This is synthetic test data.",
                ProbeCharge,
            )
            expected = {"document_no": "INV-PROBE-001", "amount": 40, "currency": "USD"}
            passed = result.model_dump(mode="python") == expected
            write_json(
                args.report,
                {
                    "scope": "synthetic_model_connectivity_not_billing_accuracy",
                    "passed": passed,
                    "result": result.model_dump(mode="python"),
                    "metadata": meta,
                },
            )
            print(
                dumps(
                    {"passed": passed, "model_call_performed": True, "requested_model": settings.model_name}
                )
            )
            return 0 if passed else 1
        elif args.command == "ocr":
            from shipping_billing.ocr.client import OCRClient, resolve_local_url

            file_url, digest = args.file_url, args.sha256
            if args.local_file:
                if not args.url_map:
                    raise BillingError(
                        "MISSING_URL_MAPPING", "Local files require an explicit downloadable URL mapping."
                    )
                protect_outputs(args.report, [args.local_file, args.url_map])
                file_url, digest = resolve_local_url(args.local_file, args.url_map, args.source_id)
            if args.replay:
                protect_outputs(args.report, [args.replay])
            result = OCRClient(settings, storage=args.storage).recognize(
                file_url,
                source_id=args.source_id,
                file_sha256=digest,
                mode="replay" if args.replay else "live",
                replay_path=args.replay,
                use_cache=args.cache,
            )
            write_json(args.report, result)
            print(
                dumps(
                    {
                        "mode": result["mode"],
                        "network_calls": result["network_calls"],
                        "text_blocks": len(result["text_blocks"]),
                        "structured_documents": len(result["documents"]),
                    }
                )
            )
        elif args.command == "schema":
            result = SCHEMAS[args.kind].json_schema(mode="validation")
            result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
            if args.output:
                write_json(args.output, result)
            else:
                print(dumps(result))
        elif args.command == "validate":
            protect_outputs(args.output, [args.input])
            model = SCHEMAS[args.kind].validate_python(load_json(args.input))
            if args.output:
                value = (
                    [m.model_dump(mode="python") for m in model]
                    if isinstance(model, list)
                    else model.model_dump(mode="python")
                )
                write_json(args.output, value)
            print(
                dumps(
                    {
                        "valid": True,
                        "kind": args.kind,
                        "records": len(model) if isinstance(model, list) else None,
                    }
                )
            )
        elif args.command == "evaluate":
            protect_outputs(
                args.report,
                [
                    args.actual,
                    args.case / "expected.json",
                    args.case / "request.json",
                    args.case / "input.eml",
                ],
            )
            report = evaluate(args.case, args.actual)
            write_json(args.report, report)
            print(
                dumps(
                    {
                        "passed": report["passed"],
                        "exact_business_records": report["exact_business_records"],
                        "pipeline_executed": False,
                    }
                )
            )
            return 0 if report["passed"] else 1
        elif args.command == "run":
            from shipping_billing.pipeline.engine import run_pipeline

            if args.context and args.accounting_entity:
                raise BillingError(
                    "CONFLICTING_CONTEXT_ARGUMENTS", "Use either context or accounting-entity."
                )
            if args.cloudflared and args.mode != "live":
                raise BillingError(
                    "INVALID_RUN_ARGUMENTS", "Temporary delivery is only available in live mode."
                )
            if args.cache_profile and args.mode == "live":
                raise BillingError(
                    "INVALID_RUN_ARGUMENTS", "Live mode selects its actual model/rule cache profile."
                )
            context = (
                ReconciliationContext.model_validate(load_json(args.context))
                if args.context
                else ReconciliationContext(
                    normalization_context=NormalizationContext(accounting_entity=args.accounting_entity)
                )
            )
            state = LedgerState.model_validate(load_json(args.state)) if args.state else LedgerState()
            if args.existing_bills:
                state = LedgerState(bills=load_json(args.existing_bills))
            local = None
            if args.local_ocr:
                from shipping_billing.parsers.local_text import LocalTextOCR

                local = LocalTextOCR()
            result = run_pipeline(
                args.inputs,
                settings=settings,
                output=args.output,
                storage=args.storage,
                mode=args.mode,
                state=state,
                context=context,
                source_id=args.source_id,
                ocr_map=args.ocr_map,
                cloudflared=args.cloudflared,
                local_ocr=local,
                cache_namespace=args.cache_profile,
                protected=[
                    p
                    for p in (args.context, args.state, args.existing_bills, args.ocr_map, args.env_file)
                    if p
                ],
                progress=lambda stage, count: LOG.info("run stage=%s items=%d", stage, count),
            )
            print(
                dumps(
                    {
                        "mode": result.mode,
                        "status": result.status,
                        **result.counts,
                        "model_invocations": result.model_invocations,
                        "ocr_network_calls": result.ocr_network_calls,
                        "billing_pipeline_executed": True,
                        "output": str(args.output),
                    }
                )
            )
            return result.exit_code
        return 0
    except ValidationError as exc:
        # Pydantic's default error string includes input values, potentially credentials.
        print(
            dumps(
                {
                    "error": {
                        "code": "VALIDATION_FAILED",
                        "message": "Input or configuration violates the contract. Online mode requires provider, model name and API key.",
                        "error_count": exc.error_count(),
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2
    except BillingError as exc:
        LOG.error("%s", exc.code)
        print(dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
        if exc.code == "PIPELINE_NOT_IMPLEMENTED":
            return 4
        if exc.code.startswith(("OCR_", "MODEL_")) and exc.code != "MODEL_NOT_CONFIGURED":
            return 3
        return 3 if exc.code == "OUTPUT_WRITE_FAILED" else 2
    except Exception:
        LOG.error("INTERNAL_ERROR")
        print(
            dumps({"error": {"code": "INTERNAL_ERROR", "message": "Unexpected internal failure."}}),
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
