"""Adversarial checks for evaluation integrity, independent of live credentials."""

import hashlib
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.stage7_fixtures import AuthoredModel, eml
from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.extraction.contracts import Citation
from shipping_billing.extraction.engine import checked_value, contains_value
from shipping_billing.extraction.units import build_units
from shipping_billing.harness.dataset import freeze_release, inside, load_split, verify_release
from shipping_billing.harness.runner import CountingClient, FixedOCR, consistency, evaluate_dataset
from shipping_billing.harness.scoring import aggregate, align, score_case
from shipping_billing.io import load_json, write_json
from shipping_billing.parsers.engine import DocumentParser
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import ReconciliationContext

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tests/fixtures/harness"


@pytest.mark.parametrize(
    "value,text,expected",
    [
        (40, "金额由40改为55。", True),
        (55, "金额由40改为55。", True),
        (40, "USD40.", True),
        (40, "40USD", True),
        (-40, "USD-40", True),
        (40, "INV-40", False),
        (40, "REF40", False),
        (40, "INV40USD", False),
        (40, "40.25", False),
        (Decimal("9007199254740993.12"), "USD9,007,199,254,740,993.12", True),
    ],
)
def test_numeric_evidence_boundaries(value, text, expected):
    assert contains_value(value, text) is expected


def test_identifier_substring_is_not_financial_evidence(tmp_path):
    source = eml(tmp_path / "invoice.eml", rows=["Invoice: INV-40"])
    parser = DocumentParser(storage=tmp_path / "parse")
    parser.parse_file(source, "s")
    units = build_units(parser.report)
    unit = next(u for u in units if u.text == "Invoice: INV-40")
    issues, evidence = [], {}
    result = checked_value(
        40,
        [Citation(unit_id=unit.unit_id, quote="40")],
        {unit.unit_id: unit},
        {"s"},
        "amount",
        issues,
        evidence,
    )
    assert result is None and issues[0].code == "UNSUPPORTED_FIELD_EVIDENCE"
    assert evidence == {}


@pytest.fixture
def prediction(tmp_path):
    source = eml(tmp_path / "input.eml")
    parser = DocumentParser(storage=tmp_path / "parse")
    parser.parse_file(source, "s")
    anchor = next(u for u in build_units(parser.report) if u.text == "Handling Fee | USD | 40 | PP")
    # Facts and desired behavior are authored before executing a prediction.
    labels = dict(
        rows=[
            dict(
                row_id="r1",
                anchors=[dict(source_id="s", locator=anchor.locator.model_dump(mode="json"))],
                expected=dict(
                    operation="CREATE",
                    document_info=dict(reference_no="INV-E2E-001"),
                    transaction_party=dict(issuer="Supplier Ltd", recipient="Buyer Ltd"),
                    charge_details=dict(
                        charge_item="Handling Charge", amount=40, currency="USD", payment_term="PP"
                    ),
                    financial_attributes=dict(accounting_entity="Buyer Ltd", posting_type="AP"),
                ),
                automatic=True,
                expected_record_id=None,
                changes=[],
                assert_values={},
            )
        ],
        expected_exit=0,
        risk_codes=[],
        old_snapshot_after=[],
        request_rejection=False,
        fault_expected=False,
        idempotence=True,
        expected_document_count=1,
    )
    folder = tmp_path / "output"
    result = run_pipeline(
        [source],
        settings=Settings(),
        source_id="s",
        mode="test_injected",
        client=AuthoredModel(),
        storage=tmp_path / "cache",
        output=folder,
        context=ReconciliationContext.model_validate(
            {"normalization_context": {"accounting_entity": "Buyer Ltd"}}
        ),
    )
    write_json(folder / "outcome.json", dict(exit_code=result.exit_code, idempotent=True, live_model_calls=0))
    case = dict(case_id="T1", split="development", family_id="test-only", formats=["eml"])
    data = dict(source_id="s", existing_bills=[])
    return case, labels, data, folder


def score(prediction):
    return score_case(*prediction)


def rewrite_artifact(folder, filename, change):
    value = load_json(folder / filename)
    change(value)
    write_json(folder / filename, value)
    manifest = load_json(folder / "manifest.json")
    manifest["artifact_sha256"][filename] = hashlib.sha256((folder / filename).read_bytes()).hexdigest()
    write_json(folder / "manifest.json", manifest)


def test_correct_prediction_and_denominator_coverage(prediction):
    result = score(prediction)
    assert result["passed"], result
    assert result["counts"]["key_correct"] == result["counts"]["key_slots"] == 6
    assert aggregate([result], require_coverage=False)["passed"]
    # A tiny happy-path test cannot claim coverage of risk cases.
    assert "risk_interception" in aggregate([result])["unmet_thresholds"]


def test_alignment_cannot_choose_favorable_amount_or_reuse_one_row():
    a = dict(source_id="s", locator={"section": "row1"})
    expected = [dict(anchors=[a], amount=40), dict(anchors=[a], amount=55)]
    actual = [dict(anchors=[a], amount=55), dict(anchors=[a], amount=40)]
    assert align(expected, actual) == {}
    assert align(expected, actual[:1]) == {}
    b = dict(source_id="s", locator={"section": "row2"})
    expected[1]["anchors"] = [b]
    actual[0]["anchors"] = [b]
    assert align(expected, actual) == {0: 1, 1: 0}


def test_missing_fault_output_still_counts_all_expected_slots(prediction):
    case, labels, data, folder = prediction
    labels["fault_expected"] = True
    (folder / "records.json").unlink()
    result = score_case(case, labels, data, folder)
    assert result["counts"]["expected"] == 1 and result["counts"]["key_slots"] == 6
    assert result["counts"]["key_correct"] == result["counts"]["operation_correct"] == 0
    assert not result["passed"] and not result["contract_valid"]


def test_changed_artifact_is_not_scored_as_valid(prediction):
    folder = prediction[-1]
    (folder / "records.json").write_bytes(b"[]")
    result = score(prediction)
    assert not result["contract_valid"] and not result["passed"]
    assert result["counts"]["expected"] == 1 and result["counts"]["predicted"] == 0


def test_omitted_null_locator_fields_have_identical_location(prediction):
    folder = prediction[-1]

    def omit_optional_fields(records):
        for refs in records[0]["audit_trail"]["field_evidence"].values():
            for ref in refs:
                ref["locator"] = {k: v for k, v in ref["locator"].items() if v is not None}

    rewrite_artifact(folder, "records.json", omit_optional_fields)
    result = score(prediction)
    assert result["passed"] and result["counts"]["evidence_correct"] == 6


def test_initialized_shared_profile_does_not_rewrite_open_index(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import shipping_billing.pipeline.engine as pipeline

    expected = pipeline.cache_profile(tmp_path, Settings(), "live")

    def reject_write(*_):
        raise AssertionError("An identical immutable profile must not be rewritten by workers")

    monkeypatch.setattr(pipeline, "write_json", reject_write)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: pipeline.cache_profile(tmp_path, Settings(), "live"), range(20)))
    assert all(result == expected for result in results)


@pytest.mark.parametrize(
    "mutation", ["wrong_amount", "forged_snapshot", "unlocatable_evidence", "invented_unknown"]
)
def test_plausible_but_wrong_outputs_fail_even_with_valid_hashes(prediction, mutation):
    folder = prediction[-1]
    if mutation == "wrong_amount":
        rewrite_artifact(folder, "records.json", lambda r: r[0]["charge_details"].update(amount=55))
    elif mutation == "forged_snapshot":
        rewrite_artifact(folder, "reconciliation.json", lambda r: r["next_state"]["bills"].clear())
    elif mutation == "unlocatable_evidence":
        rewrite_artifact(folder, "records.json", lambda r: r[0]["audit_trail"]["field_evidence"].clear())
    else:
        prediction[1]["rows"][0]["expected"]["document_info"]["reference_no"] = None
    result = score(prediction)
    assert not result["passed"]
    if mutation in ("wrong_amount", "forged_snapshot"):
        assert result["severe"]


def test_non_idempotence_is_a_severe_failure(prediction):
    folder = prediction[-1]
    write_json(folder / "outcome.json", dict(exit_code=0, idempotent=False, live_model_calls=0))
    assert dict(kind="non_idempotent") in score(prediction)["severe"]


def test_business_stability_includes_payment_and_changes(prediction):
    first = score(prediction)
    second = deepcopy(first)
    second["business_projection"][0]["business"]["/charge_details/payment_term"] = "CC"
    assert consistency([[first], [second]])["value"] == 0


def test_frozen_case_inventory_and_path_guard(tmp_path):
    folder = tmp_path / "T1"
    folder.mkdir()
    (folder / "input.eml").write_bytes(b"raw-before-inference")
    manifest = dict(
        split="development",
        cases=[dict(case_id="T1", split="development", path="T1")],
        files=[
            dict(path="T1/input.eml", sha256=hashlib.sha256((folder / "input.eml").read_bytes()).hexdigest())
        ],
    )
    write_json(tmp_path / "manifest-development.json", manifest)
    assert load_split(tmp_path, "development") == manifest
    (folder / "unfrozen.txt").write_bytes(b"unlisted")
    with pytest.raises(BillingError, match="Every case"):
        load_split(tmp_path, "development")
    with pytest.raises(BillingError):
        inside(tmp_path, "../escape")


def test_release_freezes_all_splits_and_code(tmp_path, monkeypatch):
    import shipping_billing.harness.dataset as dataset

    marker = tmp_path / "release.json"
    freeze_release(DATA, marker)
    verify_release(DATA, marker, "holdout")
    monkeypatch.setattr(dataset, "code_fingerprint", lambda: "changed-code")
    with pytest.raises(BillingError, match="Code or frozen dataset"):
        verify_release(DATA, marker, "holdout")


def test_holdout_live_requires_release_before_any_prediction(tmp_path):
    with pytest.raises(BillingError, match="Freeze code"):
        evaluate_dataset(
            DATA,
            split="holdout",
            settings=Settings(),
            mode="live",
            output=tmp_path / "out",
            storage=tmp_path / "cache",
        )
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("fault", ["OCR_TIMEOUT", "OCR_HTTP_ERROR", "OCR_INVALID_JSON", "OCR_EMPTY_CONTENT"])
def test_injected_ocr_faults_are_explicit_errors(fault):
    provider = FixedOCR({"pages": {}}, {"ocr": fault})
    with pytest.raises(BillingError) as exc:
        provider(b"page")
    assert exc.value.code == fault


@pytest.mark.parametrize("fault", ["MODEL_TIMEOUT", "MODEL_REQUEST_FAILED"])
def test_injected_model_faults_cannot_be_counted_as_network_calls(fault):
    client = CountingClient(Settings(), {"model": fault})
    with pytest.raises(BillingError) as exc:
        client.extract({})
    assert exc.value.code == fault and client.real_calls == 0 and client.injected_calls == 1
