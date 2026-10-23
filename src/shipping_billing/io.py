"""Strict JSON with exact decimal numbers and atomic file replacement."""

import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import simplejson

from shipping_billing.errors import BillingError


def _unique(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate object key")
        obj[key] = value
    return obj


def load_json(path: Path) -> Any:
    try:
        return simplejson.loads(
            path.read_text(encoding="utf-8-sig"), use_decimal=True, allow_nan=False, object_pairs_hook=_unique
        )
    except (OSError, UnicodeError, ValueError, simplejson.JSONDecodeError) as exc:
        raise BillingError(
            "INVALID_JSON_INPUT", "Cannot read a unique-key, finite UTF-8 JSON document."
        ) from exc


def _finite(value):
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("non-finite decimal")
    if isinstance(value, dict):
        for v in value.values():
            _finite(v)
    elif isinstance(value, list | tuple):
        for v in value:
            _finite(v)


def dumps(value: Any) -> str:
    _finite(value)
    return simplejson.dumps(value, use_decimal=True, allow_nan=False, ensure_ascii=False, indent=2)


def write_json(path: Path, value: Any) -> None:
    try:
        content = dumps(value) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".billing-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as exc:
        raise BillingError(
            "OUTPUT_WRITE_FAILED", "Cannot serialize or write the requested JSON output."
        ) from exc
