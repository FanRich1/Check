"""Bounded deterministic discovery; skipped material remains visible in the manifest."""

import hashlib
import os
from pathlib import Path

from shipping_billing.errors import BillingError
from shipping_billing.pipeline.contracts import DiscoveredInput

EXTENSIONS = {".eml", ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".xls", ".xlsx", ".doc", ".docx"}
PRUNED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def discover(inputs, *, excluded=(), source_id=None):
    excluded = [p.resolve() for p in excluded]
    result, selected, seen, ids = [], [], set(), {}

    def blocked(path):
        return any(path == p or path.is_relative_to(p) for p in excluded)

    def ignore(path, reason):
        result.append(DiscoveredInput(path=str(path), disposition="ignored", reason=reason))

    def add(path, explicit=False):
        path = path.resolve()
        if len(result) >= 10000:
            raise BillingError("DISCOVERY_LIMIT", "Input discovery exceeds the bounded file count.")
        if path in seen:
            ignore(path, "duplicate_input_path")
            return
        seen.add(path)
        if not explicit and path.suffix.lower() not in EXTENSIONS:
            ignore(path, "unsupported_extension")
            return
        if len(selected) >= 100:
            raise BillingError("DISCOVERY_LIMIT", "At most 100 root documents may be selected per run.")
        try:
            # Do not read oversized files merely to establish a discovery ID.
            content = (
                path.read_bytes() if path.stat().st_size <= 25 * 1024 * 1024 else str(path).encode("utf-8")
            )
        except OSError:
            content = str(path).encode("utf-8")
        key = hashlib.sha256(content).hexdigest()[:20]
        ids[key] = ids.get(key, 0) + 1
        sid = "input-" + key + ("-" + str(ids[key]) if ids[key] > 1 else "")
        selected.append((sid, path))
        result.append(DiscoveredInput(path=str(path), source_id=sid, disposition="selected"))

    for raw in inputs:
        path = raw.resolve()
        if path.is_dir():
            if blocked(path):
                raise BillingError(
                    "INPUT_STORAGE_OVERLAP", "Input directory lies inside output or cache storage."
                )

            def failed(_):
                raise BillingError("DISCOVERY_UNREADABLE", "An input directory cannot be enumerated.")

            for folder, dirs, files in os.walk(path, onerror=failed, followlinks=False):
                root = Path(folder)
                for name in sorted(dirs):
                    child = root / name
                    if name in PRUNED or child.is_symlink() or blocked(child.resolve()):
                        ignore(child, "excluded_directory")
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if d not in PRUNED and not (root / d).is_symlink() and not blocked((root / d).resolve())
                )
                for name in sorted(files):
                    child = root / name
                    if child.is_symlink() or blocked(child.resolve()):
                        ignore(child, "excluded_path")
                    else:
                        add(child)
        else:
            add(path, explicit=True)
    if not selected:
        raise BillingError("NO_INPUT_DOCUMENTS", "No input documents were selected.")
    if source_id:
        if len(selected) != 1:
            raise BillingError(
                "INVALID_SOURCE_ID", "Explicit source ID requires exactly one selected document."
            )
        selected[0] = (source_id, selected[0][1])
        next(d for d in result if d.disposition == "selected").source_id = source_id
    return selected, result
