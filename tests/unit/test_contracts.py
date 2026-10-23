from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.models import BillingRecord, BillingRequest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/synthetic"


@pytest.mark.parametrize("case", sorted(FIXTURES.glob("DEV-*")), ids=lambda p: p.name)
def test_authored_contracts(case):
    req = BillingRequest.model_validate(load_json(case / "request.json"))
    labels = load_json(case / "expected.json")
    records = TypeAdapter(list[BillingRecord]).validate_python(labels["expected_records"])
    # These are contract checks against supplied labels, not a pipeline prediction.
    assert len(records) == len(labels["expected_records"])
    assert req.documents[0].path == "input.eml"
    Draft202012Validator(BillingRequest.model_json_schema()).validate(load_json(case / "request.json"))
    Draft202012Validator(TypeAdapter(list[BillingRecord]).json_schema()).validate(labels["expected_records"])


@pytest.mark.parametrize("value", ["40", True, float("nan"), float("inf"), Decimal("NaN"), {}, []])
def test_reject_unsafe_amounts(base_record, value):
    base_record["charge_details"]["amount"] = value
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


@pytest.mark.parametrize(
    "field,value",
    [("operation", "UPSERT"), ("currency", "US"), ("payment_term", "COD"), ("document_date", "2026-02-30")],
)
def test_reject_invalid_contract_fields(base_record, field, value):
    group = {"currency": "charge_details", "payment_term": "charge_details", "document_date": "document_info"}
    if field == "operation":
        base_record[field] = value
    else:
        base_record[group[field]][field] = value
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


@pytest.mark.parametrize("field", ["amount", "currency"])
def test_automatic_decision_needs_facts(base_record, field):
    base_record["charge_details"][field] = None
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_pending_has_concrete_blocker(base_record):
    base_record["operation"] = "PENDING_REVIEW"
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_blocking_issue_cannot_be_automatically_accepted(base_record):
    base_record["audit_trail"]["issues"] = [
        {"code": "CONFLICT", "severity": "error", "blocks_automation": True, "message": "conflict"}
    ]
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_update_requires_identity_and_changes(base_record):
    base_record["operation"] = "UPDATE"
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_no_change_does_not_bypass_accounting_context(base_record):
    base_record["operation"] = "NO_CHANGE"
    base_record["existing_record_id"] = base_record["record_id"]
    base_record["financial_attributes"]["accounting_entity"] = None
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_blank_entity_is_not_a_known_entity(base_record):
    base_record["financial_attributes"]["accounting_entity"] = "  "
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_request_duplicate_old_identity_is_not_empty_history(base_request):
    old = load_json(FIXTURES / "DEV-002/request.json")["existing_bills"][0]
    base_request["existing_bills"] = [old, deepcopy(old)]
    with pytest.raises(ValidationError):
        BillingRequest.model_validate(base_request)


@pytest.mark.parametrize(
    "documents",
    [
        [],
        [{"source_id": "s", "path": "a", "url": "https://example.invalid/a"}],
        [{"source_id": "s"}],
        [{"source_id": "s", "url": "https://user:secret@example.invalid/a"}],
        [{"source_id": "s", "path": "a"}, {"source_id": "s", "path": "b"}],
    ],
)
def test_source_identity_and_location(base_request, documents):
    base_request["documents"] = documents
    with pytest.raises(ValidationError):
        BillingRequest.model_validate(base_request)


def test_snapshot_unknown_extensions_survive(base_request):
    data = load_json(FIXTURES / "DEV-004/request.json")
    data["existing_bills"][0]["erp_custom"] = {"nested": ["preserve", None]}
    parsed = BillingRequest.model_validate(data)
    assert parsed.model_dump()["existing_bills"][0]["erp_custom"] == {"nested": ["preserve", None]}


def test_unknown_root_request_typo_is_rejected(base_request):
    base_request["accounting_entitty"] = "wrong"
    with pytest.raises(ValidationError):
        BillingRequest.model_validate(base_request)


def test_evidence_must_resolve_to_registered_source(base_record):
    base_record["audit_trail"]["sources"] = []
    with pytest.raises(ValidationError):
        BillingRecord.model_validate(base_record)


def test_money_is_exact_json_number_roundtrip(base_record, tmp_path):
    amount = Decimal("9007199254740993.123456789")
    base_record["charge_details"]["amount"] = amount
    record = BillingRecord.model_validate(base_record)
    path = tmp_path / "records.json"
    write_json(path, [record.model_dump(mode="python")])
    assert "9007199254740993.123456789" in path.read_text(encoding="utf-8")
    assert '"9007199254740993.123456789"' not in path.read_text(encoding="utf-8")
    assert load_json(path)[0]["charge_details"]["amount"] == amount


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', "[NaN]", "[Infinity]", "[1,]", "{"])
def test_json_rejects_ambiguous_or_nonstandard_data(tmp_path, text):
    from shipping_billing.errors import BillingError

    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(BillingError):
        load_json(path)


def test_schema_forbids_numeric_strings(base_record):
    schema = BillingRecord.model_json_schema()
    Draft202012Validator.check_schema(schema)
    base_record["charge_details"]["amount"] = "40"
    assert list(Draft202012Validator(schema).iter_errors(base_record))


def test_nonfinite_extension_is_not_serialized(base_record):
    base_record["extensions"]["bad"] = Decimal("Infinity")
    with pytest.raises(ValueError):
        dumps(base_record)
