from copy import deepcopy

from scripts.real_material_audit import audit_run, compare_runs
from shipping_billing.io import write_json
from shipping_billing.ocr.client import ADAPTER_VERSION


def run_fixture(tmp_path):
    folder, storage = tmp_path / "run", tmp_path / "cache"
    sha, page_sha = "a" * 64, "b" * 64
    inventory = dict(sources=[dict(source_id="label", sha256=sha, kind="image", parent_id=None)])
    charge = dict(charge_item="Fee", amount=10, currency="USD")
    golden = dict(independent_human_signoff=False, cases=[dict(source_ids=["label"], fees=[dict(charge_details=charge)])])
    row = dict(operation="PENDING_REVIEW", document_info={}, transaction_party={}, charge_details=charge,
               financial_attributes={}, audit_trail=dict(sources=["input"], field_evidence={"amount": ["source quote"]}))
    file = dict(source_id="input", sha256=sha, original_name="authored.png", parent_source_id=None,
                status="pending_review", issues=[], stages=dict(extraction="extracted"))
    write_json(folder / "manifest.json", dict(mode="live", status="pending_review", files=[file],
                                              model_invocations=1, ocr_network_calls=1))
    write_json(folder / "records.json", [row])
    write_json(folder / "parse.json", dict(sources=[dict(source_id="input", sha256=sha, original_name="authored.png",
                 metadata=dict(ocr_page_status=[dict(page=1, status="completed", mode="live")]))]))
    archive = tmp_path / "archive.json"
    write_json(archive, dict(source_id="input:page-1", file_sha256=page_sha, http_status=200,
                            adapter_version=ADAPTER_VERSION, task="orderChange",
                            response=dict(success=True, data=dict(text="Fee USD 10"))))
    write_json(folder / "ocr-replay-map.json", {"input:page-1": dict(sha256=page_sha, archive=str(archive))})
    write_json(storage / "parsing/events/one.json", dict(source_id="input:page-1", status="completed",
                                                         network_calls=1, elapsed_seconds=1, result_mode="live"))
    return folder, storage, inventory, golden, row


def test_audit_separates_business_success_online_coverage_and_independent_signoff(tmp_path):
    folder, storage, inventory, golden, _ = run_fixture(tmp_path)
    report = audit_run(folder, inventory, golden, storage=storage)
    assert report["passed"] and report["page_archives_complete"] and report["complete_online_success"]
    assert not report["independent_accuracy_acceptance_complete"]
    assert not report["independent_business_signoff"]
    cached = audit_run(folder, inventory, golden)
    assert cached["passed"] and not cached["complete_online_success"]


def test_same_fee_count_does_not_hide_wrong_amount_or_currency(tmp_path):
    folder, storage, inventory, golden, row = run_fixture(tmp_path)
    row = deepcopy(row)
    row["charge_details"].update(amount=11, currency="CNY")
    write_json(folder / "records.json", [row])
    report = audit_run(folder, inventory, golden, storage=storage)
    assert not report["passed"] and report["actual_fees"] == report["expected_fees"] == 1
    assert report["differences"][0]["missing"][0]["amount"] == 10
    assert report["differences"][0]["extra"][0]["currency"] == "CNY"


def test_missing_archive_or_ocr_page_is_not_a_complete_online_success(tmp_path):
    folder, storage, inventory, golden, _ = run_fixture(tmp_path)
    write_json(folder / "ocr-replay-map.json", {})
    write_json(folder / "parse.json", dict(sources=[dict(source_id="input", sha256="a" * 64, original_name="authored.png",
                 metadata=dict(ocr_page_status=[dict(page=1, status="error", code="OCR_EMPTY_CONTENT")]))]))
    report = audit_run(folder, inventory, golden, storage=storage)
    assert report["passed"] and not report["page_archives_complete"]
    assert report["usable_ocr_pages"] == 0 and not report["complete_online_success"]


def test_business_replay_comparison_includes_evidence_and_operations(tmp_path):
    folder, _, _, _, row = run_fixture(tmp_path)
    second = tmp_path / "second"
    write_json(second / "records.json", [row])
    assert compare_runs(folder, second)["business_and_evidence_identical"]
    row["audit_trail"]["field_evidence"]["amount"] = ["another quote"]
    write_json(second / "records.json", [row])
    assert not compare_runs(folder, second)["business_and_evidence_identical"]
