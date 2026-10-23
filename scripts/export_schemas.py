"""Export structural contracts; semantic invariants remain in the Python validators."""

from pathlib import Path

from shipping_billing.cli import SCHEMAS
from shipping_billing.io import write_json

ROOT = Path(__file__).resolve().parents[1]

for kind, adapter in SCHEMAS.items():
    schema = adapter.json_schema(mode="validation")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    write_json(ROOT / "schemas" / f"{kind}.schema.json", schema)
print("Exported structural schemas: " + ", ".join(SCHEMAS))
