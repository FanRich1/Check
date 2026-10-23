"""Bounded recursive dispatch; one source entry for each encountered attachment."""

import hashlib
from pathlib import Path

from shipping_billing.errors import BillingError
from shipping_billing.parsers.contracts import ParsedSource, ParseLimits, ParseReport
from shipping_billing.parsers.email import parse_email
from shipping_billing.parsers.formats import detect, inspect_zip
from shipping_billing.parsers.legacy_word import convert_doc
from shipping_billing.parsers.media import parse_image, parse_pdf
from shipping_billing.parsers.office import parse_docx, parse_xls, parse_xlsx


class DocumentParser:
    def __init__(
        self,
        *,
        storage: Path,
        mode="offline",
        ocr_provider=None,
        limits=None,
        doc_converter=None,
        local_ocr=None,
    ):
        self.storage = storage
        self.limits = limits or ParseLimits()
        self.ocr_provider = ocr_provider
        self.doc_converter = doc_converter or convert_doc
        self.local_ocr = local_ocr
        self.report = ParseReport(mode=mode)
        self.total_bytes = 0

    def parse_file(self, path: Path, source_id: str):
        if any(s.source_id == source_id for s in self.report.sources):
            raise BillingError("DUPLICATE_SOURCE_ID", "Each root source must have a unique ID.")
        size = None
        try:
            size = path.stat().st_size
            if size > self.limits.max_file_bytes:
                raise BillingError("FILE_SIZE_LIMIT", "Input exceeds configured per-file size limit.")
            data = path.read_bytes()
        except (OSError, BillingError) as exc:
            source = ParsedSource(source_id=source_id, original_name=path.name, sha256=None, size_bytes=size)
            source.issue(
                exc.code if isinstance(exc, BillingError) else "SOURCE_UNREADABLE",
                "Source could not be read within configured limits.",
                error=True,
            )
            self.report.sources.append(source)
            return source
        return self.parse_bytes(data, source_id, path.name)

    def parse_bytes(self, data, source_id, name, *, parent=None, mime=None, depth=0, metadata=None):
        source = ParsedSource(
            source_id=source_id,
            parent_source_id=parent,
            original_name=name,
            declared_mime=mime,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            metadata=metadata or {},
        )
        self.report.sources.append(source)
        self.total_bytes += len(data)
        try:
            if (
                len(self.report.sources) > self.limits.max_sources
                or self.total_bytes > self.limits.max_total_bytes
            ):
                raise BillingError(
                    "DOCUMENT_BUDGET_EXCEEDED", "Document count or aggregate byte limit exceeded."
                )
            if depth > self.limits.max_depth:
                raise BillingError("NESTING_LIMIT", "Nested attachment depth exceeds configured limit.")
            if len(data) > self.limits.max_file_bytes:
                raise BillingError("FILE_SIZE_LIMIT", "Attachment exceeds configured per-file size limit.")
            kind = detect(data, self.limits)
            source.detected_format = kind
            if kind == "unknown":
                raise BillingError(
                    "UNSUPPORTED_FORMAT", "File signature does not identify a supported document."
                )
            folder = self.storage / "sources"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{source.sha256}.{kind}"
            if not path.exists():
                path.write_bytes(data)
            source.metadata["retained_file"] = str(path.resolve())
            suffix = Path(name).suffix.lower().lstrip(".")
            if suffix and suffix not in (
                {kind} | ({"jpeg"} if kind == "jpg" else {"tif"} if kind == "tiff" else set())
            ):
                source.issue(
                    "FILE_EXTENSION_MISMATCH",
                    "Detected file signature differs from supplied filename.",
                    blocking=False,
                )

            def visit_child(raw, child_name, child_mime, child_key, child_meta):
                if len(self.report.sources) >= self.limits.max_sources:
                    source.issue(
                        "DOCUMENT_BUDGET_EXCEEDED",
                        "Further attachments were not expanded after the source limit.",
                        error=True,
                    )
                    source.metadata["unexpanded_attachment_count"] = (
                        source.metadata.get("unexpanded_attachment_count", 0) + 1
                    )
                    return
                self.parse_bytes(
                    raw,
                    f"{source_id}/{child_key}",
                    child_name,
                    parent=source_id,
                    mime=child_mime,
                    depth=depth + 1,
                    metadata=child_meta,
                )

            if kind == "eml":
                parse_email(data, source, visit_child)
            elif kind == "pdf":
                parse_pdf(data, source, self.limits, self.ocr_provider, self.local_ocr)
            elif kind in ("png", "jpg", "tiff"):
                parse_image(data, source, self.limits, self.ocr_provider, self.local_ocr)
            elif kind == "xlsx":
                parse_xlsx(data, source, self.limits)
            elif kind == "xls":
                parse_xls(data, source, self.limits)
            elif kind == "docx":
                parse_docx(data, source, self.limits, visit_child)
            elif kind == "doc":
                converted = self.doc_converter(data, self.storage / "conversion")
                inspect_zip(converted, self.limits)
                source.metadata["conversion"] = {
                    "engine": "Microsoft Word",
                    "target": "docx",
                    "output_sha256": hashlib.sha256(converted).hexdigest(),
                }
                parse_docx(converted, source, self.limits, visit_child)
        except BillingError as exc:
            source.issue(exc.code, exc.message, error=True)
        except Exception:
            source.issue(
                "DOCUMENT_PARSE_FAILED",
                "Document could not be parsed; no content was silently accepted.",
                error=True,
            )
        children = [s for s in self.report.sources if s.parent_source_id == source_id]
        if any(s.status in ("error", "pending_review") for s in children):
            source.issue(
                "CHILD_REQUIRES_REVIEW", "At least one attachment has incomplete or failed extraction."
            )
        return source
