from copy import deepcopy

import pytest
from pydantic import ValidationError

from scripts.stage6_cases import scenarios, snapshot
from shipping_billing.domain.engine import normalize
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.reconciliation.contracts import LedgerState
from shipping_billing.reconciliation.engine import apply_records, reconcile

CASES = scenarios()


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_stage6_business_and_replay(case, tmp_path):
    _, source, ctx, state, expected = deepcopy(case)
    original = state.model_dump(mode="python")
    source_before = source.model_dump(mode="python")
    normalized = normalize(source, ctx.normalization_context)
    result = reconcile(normalized, source, state=state, context=ctx)
    assert [d.operation for d in result.decisions] == expected["operations"], [
        (d.match_rule, [i.code for i in d.issues]) for d in result.decisions
    ]
    assert sorted(c.path for r in result.records for c in r.audit_trail.changes) == sorted(expected["paths"])
    if expected.get("rule"):
        assert result.decisions[0].match_rule == expected["rule"]
    if expected.get("target"):
        assert result.decisions[0].record_id == expected["target"]
    if expected.get("issue"):
        assert any(i.code == expected["issue"] for d in result.decisions for i in d.issues)
    assert state.model_dump(mode="python") == original
    assert source.model_dump(mode="python") == source_before
    # Also prove JSON roundtrip, not only reuse of Python model objects.
    path = tmp_path / "state.json"
    write_json(path, result.next_state.model_dump(mode="python"))
    restored = LedgerState.model_validate(load_json(path))
    again = reconcile(normalized, source, state=restored, context=ctx)
    assert all(d.operation not in ("CREATE", "UPDATE") for d in again.decisions)
    assert again.next_state == restored
    if all(op == "PENDING_REVIEW" for op in expected["operations"]):
        assert result.next_state == state
    assert len(result.next_state.bills) == len(state.bills) + sum(
        r.operation == "CREATE" for r in result.records
    )
    # Old custom fields must survive all four operations and all replay paths.
    by_id = {b.record_id: b.model_dump(mode="python", exclude_unset=True) for b in result.next_state.bills}
    for old in original["bills"]:
        assert by_id[old["record_id"]]["erp_metadata"] == old["erp_metadata"]
        assert by_id[old["record_id"]]["charge_details"]["erp_tax_code"] == "T0"
        assert by_id[old["record_id"]]["document_info"]["custom_document_tag"] == "original"
    assert "NaN" not in dumps(result.model_dump(mode="python"))


def test_invalid_duplicate_old_record_ids_rejected():
    with pytest.raises(ValidationError):
        LedgerState(bills=[snapshot(), snapshot()])


def test_forged_normalized_values_rejected():
    _, source, ctx, state, _ = deepcopy(CASES[0])
    normalized = normalize(source, ctx.normalization_context)
    normalized.lines[0].candidate.charge_details.amount = 999
    with pytest.raises(BillingError, match="Normalization"):
        reconcile(normalized, source, context=ctx, state=state)


def test_apply_rejects_stale_and_undeclared_updates():
    case = next(c for c in CASES if c[0] == "authorized-amount")
    _, source, ctx, state, _ = deepcopy(case)
    result = reconcile(normalize(source, ctx.normalization_context), source, state=state, context=ctx)
    old = state.model_dump(mode="python")["bills"]
    old[0]["charge_details"]["amount"] = 41
    with pytest.raises(BillingError, match="snapshot changed"):
        apply_records(old, result.records)
    old[0]["charge_details"]["amount"] = 40
    result.records[0].extensions["unexpected"] = True
    with pytest.raises(BillingError, match="outside"):
        apply_records(old, result.records)


def test_parse_hash_belongs_to_extraction_and_supports_rename():
    from shipping_billing.extraction.engine import digest
    from shipping_billing.parsers.contracts import ParsedSource, ParseReport

    _, source, ctx, state, _ = deepcopy(CASES[0])
    parsed = ParseReport(
        mode="offline",
        sources=[ParsedSource(source_id="s", original_name="first.eml", sha256="a" * 64, size_bytes=123)],
    )
    normalized = normalize(source, ctx.normalization_context)
    with pytest.raises(BillingError, match="Source hashes"):
        reconcile(normalized, source, context=ctx, parsed=parsed)
    source.input_sha256 = digest(parsed.model_dump(mode="python"))
    result = reconcile(normalize(source, ctx.normalization_context), source, context=ctx, parsed=parsed)
    assert result.records[0].operation == "CREATE"
    parsed.sources[0].original_name = "renamed.eml"
    source.input_sha256 = digest(parsed.model_dump(mode="python"))
    again = reconcile(
        normalize(source, ctx.normalization_context),
        source,
        state=result.next_state,
        context=ctx,
        parsed=parsed,
    )
    assert again.records[0].operation == "NO_CHANGE"
    assert again.next_state == result.next_state


def test_old_field_presence_and_history_preserved():
    _, source, ctx, state, _ = deepcopy(next(c for c in CASES if c[0] == "authorized-amount"))
    raw = state.model_dump(mode="python")
    del raw["bills"][0]["charge_details"]["unit_price"]
    raw["bills"][0]["operation"] = "HISTORICAL"
    raw["bills"][0]["audit_trail"] = {"legacy": "keep"}
    state = LedgerState.model_validate(raw)
    result = reconcile(normalize(source, ctx.normalization_context), source, state=state, context=ctx)
    bill = result.next_state.model_dump(mode="python")["bills"][0]
    assert "unit_price" not in bill["charge_details"]
    assert bill["operation"] == "HISTORICAL" and bill["audit_trail"] == {"legacy": "keep"}
