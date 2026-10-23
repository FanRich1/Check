"""Parsing output is evidence, never an accepted billing decision."""

from typing import Any, Literal

from pydantic import Field

from shipping_billing.models import Contract, DocumentFragment, Issue, Locator, Text


class ParsedFragment(DocumentFragment):
    method: Text


class ParsedTable(Contract):
    table_id: Text
    locator: Locator
    rows: list[list[Any]]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedSource(Contract):
    source_id: Text
    parent_source_id: Text | None = None
    original_name: str
    declared_mime: str | None = None
    detected_format: str = "unknown"
    sha256: str | None
    size_bytes: int | None
    status: Literal["parsed", "pending_review", "error"] = "parsed"
    fragments: list[ParsedFragment] = Field(default_factory=list)
    tables: list[ParsedTable] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ocr_results: list[dict[str, Any]] = Field(default_factory=list)

    def issue(self, code, message, *, error=False, blocking=True):
        self.issues.append(
            Issue(
                code=code,
                message=message,
                severity="error" if error else "warning",
                blocks_automation=blocking,
            )
        )
        if error:
            self.status = "error"
        elif blocking and self.status != "error":
            self.status = "pending_review"

    def fragment(self, text, locator, method):
        self.fragments.append(
            ParsedFragment(
                fragment_id=f"{self.source_id}:f{len(self.fragments) + 1}",
                source_id=self.source_id,
                text=text,
                locator=locator,
                method=method,
            )
        )


class ParseReport(Contract):
    schema_version: Literal["0.1.0"] = "0.1.0"
    mode: Literal["offline", "live", "replay"]
    sources: list[ParsedSource] = Field(default_factory=list)
    billing_pipeline_executed: Literal[False] = False


class ParseLimits(Contract):
    max_file_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_total_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_sources: int = Field(default=100, gt=0)
    max_depth: int = Field(default=5, ge=0, le=20)
    max_pages: int = Field(default=100, gt=0)
    max_cells: int = Field(default=100000, gt=0)
    max_zip_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=25000000, gt=0)
