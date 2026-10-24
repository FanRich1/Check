"""Identical ledger acceptance tests on portable SQLite and optional real MySQL.

Set BILLING_TEST_MYSQL_URL to a dedicated test database; never use a production database.
All fixtures use unique ledger IDs and injected extraction, without external OCR/model calls.
"""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from scripts.stage7_fixtures import AuthoredModel, eml
from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.reconciliation.contracts import LedgerState
from shipping_billing.storage.database import (
    LedgerRepository,
    amount_value,
    bills,
    documents,
    observations,
    stamp,
)
from shipping_billing.web.server import create_app
from shipping_billing.web.service import JobStore

ONLINE = dict(mode="online", model_provider="deepseek", model_name="test", model_api_key="test-only")
TEST_URL = SecretStr(os.environ.get("BILLING_TEST_MYSQL_URL", ""))


def pipeline(paths, **kwargs):
    return run_pipeline(paths, **{**kwargs, "mode": "test_injected", "client": AuthoredModel()})


@pytest.fixture(params=["sqlite", "mysql"] if TEST_URL.get_secret_value() else ["sqlite"])
def repository(request, tmp_path):
    url = TEST_URL.get_secret_value() if request.param == "mysql" else (
        "sqlite+pysqlite:///" + (tmp_path / "ledger.sqlite").as_posix()
    )
    repo = LedgerRepository(url, allow_sqlite=request.param == "sqlite")
    yield repo
    repo.close()


@pytest.fixture
def web(repository, tmp_path):
    app = create_app(tmp_path, Settings(**ONLINE), repository=repository, pipeline=pipeline)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["x-workspace-token"] = client.get("/api/config").json()["token"]
        book = client.post("/api/ledgers", json={"name": "验收账本", "entity": "Buyer Ltd"}).json()
        yield client, app.state.store, book


def upload(client, book, raw):
    response = client.post("/api/jobs", data={"ledger_id": book["id"]},
                           files={"documents": ("invoice.eml", raw, "message/rfc822")})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def finish(client, job_id):
    import time

    for _ in range(300):
        response = client.get("/api/jobs/" + job_id)
        assert response.status_code == 200, response.text
        value = response.json()
        if value["job"]["status"] not in ("queued", "running"):
            return value
        time.sleep(.02)
    raise AssertionError("Job did not complete")


def state_fixture():
    return LedgerState.model_validate(load_json(Path("tests/fixtures/reconciliation/example-old-state.json")))


def job(repository, book, revision):
    item = dict(id=uuid4().hex, ledger_id=book["id"], ledger_base_revision=revision,
                created_at=stamp(), status="running", ledger_commit_status="pending")
    repository.save_job(item)
    return {**item, "status": "completed"}


def test_automatic_create_repeat_update_and_restart(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "new.eml").read_bytes()
    first = finish(client, upload(client, book, raw))
    assert first["records"][0]["operation"] == "CREATE"
    assert first["job"]["ledger_commit_status"] == "committed"
    second = finish(client, upload(client, book, raw))
    assert second["records"][0]["operation"] == "NO_CHANGE"
    original_id = first["records"][0]["record_id"]
    update = eml(tmp_path / "update.eml", 50, note="Update amount from 40 to 50").read_bytes()
    third = finish(client, upload(client, book, update))
    assert third["records"][0]["operation"] == "UPDATE"
    assert third["records"][0]["record_id"] == original_id
    fourth = finish(client, upload(client, book, update))
    assert fourth["records"][0]["operation"] == "NO_CHANGE"
    fresh = LedgerRepository(store.repository.engine.url, allow_sqlite=True)
    try:
        state, revision = fresh.snapshot(book["id"])
        assert revision == 4 and len(state.bills) == 1
        assert state.bills[0].charge_details.amount == Decimal(50)
        assert fresh.get_job(fourth["job"]["id"])["ledger_revision"] == 4
        with fresh.transaction() as connection:
            assert connection.scalar(select(func.count()).select_from(observations)
                                     .where(observations.c.job_id == third["job"]["id"])) == 1
            assert connection.scalar(select(func.count()).select_from(documents)
                                     .where(documents.c.job_id == third["job"]["id"])) == 1
    finally:
        fresh.close()


def test_pending_and_checked_reviews_do_not_change_accepted_amount(web, tmp_path):
    client, store, book = web
    finish(client, upload(client, book, eml(tmp_path / "first.eml").read_bytes()))
    pending = finish(client, upload(client, book, eml(tmp_path / "different.eml", 70).read_bytes()))
    assert pending["records"][0]["operation"] == "PENDING_REVIEW"
    record_id = pending["records"][0]["record_id"]
    response = client.post(f"/api/jobs/{pending['job']['id']}/review",
                           json={"record_id": record_id, "status": "checked", "note": "已核对，未授权改账"})
    assert response.status_code == 200
    state, _ = store.repository.snapshot(book["id"])
    assert len(state.bills) == 1 and state.bills[0].charge_details.amount == 40
    assert store.repository.get_reviews(pending["job"]["id"])[record_id]["status"] == "checked"


def test_same_name_separate_lines_are_not_collapsed(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "lines.eml", rows=["Handling Fee | USD | 65 | PP", "Handling Fee | USD | 35 | PP"]).read_bytes()
    first = finish(client, upload(client, book, raw))
    assert [r["operation"] for r in first["records"]] == ["CREATE", "CREATE"]
    repeat = finish(client, upload(client, book, raw))
    assert all(r["operation"] == "NO_CHANGE" for r in repeat["records"])
    state, _ = store.repository.snapshot(book["id"])
    assert sorted(b.charge_details.amount for b in state.bills) == [35, 65]


def test_import_export_precision_extensions_isolation_and_no_overwrite(web):
    client, store, book = web
    state = state_fixture()
    state.bills[0].charge_details.amount = Decimal("9007199254740993.12")
    state = LedgerState.model_validate({**state.model_dump(), "identities": [
        {"record_id": state.bills[0].record_id, "occurrence_keys": []}
    ]})
    encoded = dumps(state.model_dump(mode="python"))
    response = client.post(f"/api/ledgers/{book['id']}/import", data={"revision": "0"},
                           files={"state": ("old.json", encoded, "application/json")})
    assert response.status_code == 200, response.text
    exported = client.get(f"/api/ledgers/{book['id']}/export")
    assert exported.text == encoded
    detail = client.get(f"/api/ledgers/{book['id']}").json()
    assert detail["bills"][0]["amount_display"] == "9007199254740993.12"
    other = store.repository.create_ledger("另一个账本", "Other Ltd")
    assert store.repository.snapshot(other["id"])[0].bills == []
    with pytest.raises(BillingError) as exc:
        store.repository.import_state(other["id"], state, 0)
    assert exc.value.code == "LEDGER_ENTITY_CONFLICT"
    assert store.repository.snapshot(other["id"])[1] == 0
    response = client.post(f"/api/ledgers/{book['id']}/import", data={"revision": "1"},
                           files={"state": ("old.json", encoded, "application/json")})
    assert response.status_code == 409 and response.json()["error"]["code"] == "LEDGER_NOT_EMPTY"


def test_stale_commit_rejected_and_receipt_is_idempotent(repository):
    book = repository.create_ledger("conflict", "Buyer Ltd")
    first, stale = job(repository, book, 0), job(repository, book, 0)
    state = state_fixture()
    receipt = repository.commit_job(first, state, [], [])
    assert receipt["ledger_revision"] == 1
    assert repository.commit_job(first, LedgerState(), [], []) == receipt
    with pytest.raises(BillingError) as exc:
        repository.commit_job(stale, LedgerState(), [], [])
    assert exc.value.code == "LEDGER_CHANGED"
    assert repository.snapshot(book["id"])[0].model_dump() == state.model_dump()
    assert repository.get_job(stale["id"])["ledger_commit_status"] == "pending"


def test_failed_receipt_rolls_back_bills_revision_and_observations(repository, monkeypatch):
    book = repository.create_ledger("rollback", "Buyer Ltd")
    repository.import_state(book["id"], state_fixture(), 0)
    before, revision = repository.snapshot(book["id"])
    proposal = deepcopy(before)
    proposal.bills[0].charge_details.amount = Decimal(50)
    item = job(repository, book, revision)

    def failure(*args):
        raise BillingError("TEST_WRITE_FAILURE", "Injected failure after ledger writes")

    monkeypatch.setattr(repository, "_results", failure)
    with pytest.raises(BillingError):
        repository.commit_job(item, proposal, [], [])
    after, after_revision = repository.snapshot(book["id"])
    assert after.model_dump() == before.model_dump() and after_revision == revision
    assert repository.get_job(item["id"])["status"] == "running"


def test_real_mysql_concurrent_commits_only_one_wins(repository):
    if repository.backend != "mysql":
        pytest.skip("Real row-lock test requires MySQL")
    book = repository.create_ledger("concurrent", "Buyer Ltd")
    items = [job(repository, book, 0), job(repository, book, 0)]
    barrier = threading.Barrier(2)

    def commit(item):
        peer = LedgerRepository(repository.engine.url)
        try:
            barrier.wait(timeout=10)
            try:
                peer.commit_job(item, state_fixture(), [], [])
                return "committed"
            except BillingError as exc:
                return exc.code
        finally:
            peer.close()

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(commit, items))
    assert sorted(results) == ["LEDGER_CHANGED", "committed"]
    assert repository.snapshot(book["id"])[1] == 1


def test_queued_job_reads_latest_ledger(web, tmp_path):
    client, store, book = web
    release, started = threading.Event(), threading.Event()
    calls = []

    def blocking(paths, **kwargs):
        calls.append(len(kwargs["state"].bills))
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
        return pipeline(paths, **kwargs)

    store.pipeline = blocking
    raw = eml(tmp_path / "new.eml").read_bytes()
    first = upload(client, book, raw)
    try:
        assert started.wait(5)
        second = upload(client, book, raw)
    finally:
        release.set()
    assert finish(client, first)["records"][0]["operation"] == "CREATE"
    assert finish(client, second)["records"][0]["operation"] == "NO_CHANGE"
    assert calls == [0, 1]


def test_worker_conflict_preserves_committed_ledger_and_blocks_state_download(web, tmp_path):
    client, store, book = web

    def concurrent_update(paths, **kwargs):
        result = pipeline(paths, **kwargs)
        peer = LedgerRepository(store.repository.engine.url, allow_sqlite=True)
        try:
            peer.import_state(book["id"], state_fixture(), 0)
        finally:
            peer.close()
        return result

    store.pipeline = concurrent_update
    result = finish(client, upload(client, book, eml(tmp_path / "conflict.eml").read_bytes()))
    assert result["job"]["ledger_commit_status"] == "conflict"
    assert result["job"]["error_code"] == "LEDGER_CHANGED"
    assert result["job"]["status"] == "pending_review"
    assert "state.json" not in result["downloads"]
    assert client.get(f"/api/jobs/{result['job']['id']}/download/state.json").status_code == 404
    state, revision = store.repository.snapshot(book["id"])
    assert revision == 1 and state.model_dump() == state_fixture().model_dump()


def test_database_mode_requires_ledger_and_rejects_mixed_inputs(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "new.eml").read_bytes()
    files = {"documents": ("invoice.eml", raw, "message/rfc822")}
    assert client.post("/api/jobs", data={"entity": "Buyer Ltd"}, files=files).json()["error"]["code"] == "LEDGER_REQUIRED"
    response = client.post("/api/jobs", data={"ledger_id": book["id"], "entity": "Other Ltd"}, files=files)
    assert response.status_code == 409
    response = client.post("/api/jobs", data={"ledger_id": book["id"], "base_job": "a" * 32}, files=files)
    assert response.json()["error"]["code"] == "CONFLICTING_OLD_STATE"
    assert not store.active


def test_old_job_migrates_as_history_without_automatic_booking(repository, tmp_path):
    file_store = JobStore(tmp_path, Settings(**ONLINE), pipeline=pipeline)
    raw = eml(tmp_path / "old.eml").read_bytes()
    item = file_store.submit([("old.eml", raw)], entity="Buyer Ltd")
    file_store.close()
    record = file_store.result(item["id"])["records"][0]
    file_store.review(item["id"], record["record_id"], "checked", "historical")
    migrated = JobStore(tmp_path, Settings(**ONLINE), repository=repository)
    try:
        assert migrated.get(item["id"])["status"] == "completed"
        assert repository.get_reviews(item["id"])[record["record_id"]]["note"] == "historical"
        assert not repository.get_job(item["id"]).get("ledger_id")
        with repository.transaction() as connection:
            assert connection.scalar(select(func.count()).select_from(observations)
                                     .where(observations.c.job_id == item["id"])) == 1
    finally:
        migrated.close()


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "1E35", "1E-31"])
def test_out_of_range_amounts_rejected_before_sql(amount):
    with pytest.raises(BillingError) as exc:
        amount_value(amount)
    assert exc.value.code == "DATABASE_AMOUNT_RANGE"


def test_invalid_pending_state_rejected_before_mutation(repository):
    book = repository.create_ledger("pending import", "Buyer Ltd")
    state = state_fixture()
    state.bills[0].__pydantic_extra__["operation"] = "PENDING_REVIEW"
    with pytest.raises(BillingError) as exc:
        repository.import_state(book["id"], state, 0)
    assert exc.value.code == "PENDING_BILL_IMPORT"
    assert repository.snapshot(book["id"])[1] == 0


def test_sql_amount_column_is_exact(repository):
    book = repository.create_ledger("decimal", "Buyer Ltd")
    state = state_fixture()
    amount = Decimal("9007199254740993.12345678901234567890123456789")
    state.bills[0].charge_details.amount = amount
    repository.import_state(book["id"], state, 0)
    with repository.transaction() as connection:
        assert connection.scalar(select(bills.c.amount).where(bills.c.ledger_id == book["id"])) == amount


def test_no_silent_database_fallback_or_password_disclosure(tmp_path):
    secret = "should-never-appear"
    settings = Settings(database_url=f"mysql+pymysql://nobody:{secret}@127.0.0.1:1/absent")
    with pytest.raises(BillingError) as exc:
        create_app(tmp_path, settings)
    assert exc.value.code.startswith("DATABASE_")
    assert secret not in str(exc.value) and secret not in repr(exc.value)


def test_production_rejects_sqlite():
    with pytest.raises(BillingError) as exc:
        LedgerRepository("sqlite+pysqlite:///:memory:")
    assert exc.value.code == "DATABASE_CONFIG_INVALID"


def review_request(client, book, result, *, action="create", target=None, fields=None):
    rid = result["records"][0]["record_id"]
    workspace = client.get(f"/api/jobs/{result['job']['id']}/review-workspace",
                           params={"record_id": rid, "ledger_id": book["id"]})
    assert workspace.status_code == 200, workspace.text
    return dict(record_id=rid, ledger_id=book["id"], revision=workspace.json()["ledger"]["revision"],
                action=action, target_record_id=target, fields=fields or {}, payer="Buyer Ltd", payee="Supplier Ltd",
                reviewer="人工验收员", reason="已对照原件确认金额、币种和交易关系", distinct=action == "create")


def approve(client, result, body):
    base = f"/api/jobs/{result['job']['id']}"
    preview = client.post(base + "/review-preview", json=body)
    assert preview.status_code == 200, preview.text
    response = client.post(base + "/review-approve", json={**body, "preview_token": preview.json()["preview_token"],
                                                           "confirmed": True})
    assert response.status_code == 200, response.text
    return response.json(), preview.json()["preview_token"]


def test_human_create_exact_reupload_receipt_and_originals_immutable(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "ambiguous.eml", currency="$").read_bytes()
    result = finish(client, upload(client, book, raw))
    assert result["records"][0]["operation"] == "PENDING_REVIEW"
    folder = store.folder(result["job"]["id"]) / "result"
    frozen = {name: (folder / name).read_bytes() for name in ("records.json", "state.json", "audit.json", "manifest.json")}
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    receipt, token = approve(client, result, body)
    assert receipt["after"]["amount_display"] == "40"
    assert all((folder / name).read_bytes() == value for name, value in frozen.items())
    state, revision = store.repository.snapshot(book["id"])
    assert len(state.bills) == 1 and state.bills[0].charge_details.currency == "USD"
    again = client.post(f"/api/jobs/{result['job']['id']}/review-approve",
                        json={**body, "preview_token": token, "confirmed": True})
    assert again.status_code == 200 and again.json()["decision_id"] == receipt["decision_id"]
    assert store.repository.snapshot(book["id"])[1] == revision
    repeat = finish(client, upload(client, book, raw))
    assert repeat["records"][0]["operation"] == "NO_CHANGE"
    assert len(store.repository.snapshot(book["id"])[0].bills) == 1
    exported = client.get(f"/api/jobs/{result['job']['id']}/download/review-decisions.json")
    assert exported.status_code == 200 and "人工验收员" in exported.text
    original = client.get(f"/api/jobs/{result['job']['id']}/sources/{result['files'][0]['source_id']}/download")
    assert original.status_code == 200 and original.content == raw
    fresh = LedgerRepository(store.repository.engine.url, allow_sqlite=True)
    try:
        assert fresh.get_decisions(result["job"]["id"])[body["record_id"]]["decision_id"] == receipt["decision_id"]
    finally:
        fresh.close()


def test_human_update_inherits_unknown_fields_and_uses_incoming_values(web, tmp_path):
    client, store, book = web
    state = state_fixture()
    state.bills[0].document_info.reference_no = "INV-E2E-001"
    store.repository.import_state(book["id"], state, 0)
    result = finish(client, upload(client, book, eml(tmp_path / "conflict.eml", 70).read_bytes()))
    body = review_request(client, book, result, action="update", target="OLD1", fields={"/charge_details/amount": "70"})
    workspace = client.get(f"/api/jobs/{result['job']['id']}/review-workspace",
                           params={"record_id": body["record_id"], "ledger_id": book["id"]}).json()
    assert workspace["suggested"]["/charge_details/amount"] == "70"
    receipt, _ = approve(client, result, body)
    after = store.repository.snapshot(book["id"])[0].bills[0].model_dump(mode="python", exclude_unset=True)
    before = state.bills[0].model_dump(mode="python", exclude_unset=True)
    before["charge_details"]["amount"] = Decimal(70)
    assert after == before
    assert receipt["changes"] == [dict(path="/charge_details/amount", old=40, new=70, old_display="40", new_display="70")]


@pytest.mark.parametrize("action", ["duplicate", "reject"])
def test_human_duplicate_or_reject_keeps_original_financial_values(web, tmp_path, action):
    client, store, book = web
    first = finish(client, upload(client, book, eml(tmp_path / "first.eml").read_bytes()))
    raw = eml(tmp_path / "conflict.eml", 70).read_bytes()
    result = finish(client, upload(client, book, raw))
    before, _ = store.repository.snapshot(book["id"])
    body = review_request(client, book, result, action=action,
                          target=first["records"][0]["record_id"] if action == "duplicate" else None)
    approve(client, result, body)
    after, _ = store.repository.snapshot(book["id"])
    assert [b.model_dump() for b in after.bills] == [b.model_dump() for b in before.bills]
    assert client.get(f"/api/jobs/{result['job']['id']}").json()["job"]["review_pending_count"] == 0
    if action == "duplicate":
        assert finish(client, upload(client, book, raw))["records"][0]["operation"] == "NO_CHANGE"


@pytest.mark.parametrize("fields,payer,payee", [
    ({"/charge_details/amount": "NaN"}, "Buyer Ltd", "Supplier Ltd"),
    ({"/charge_details/amount": "1.001", "/charge_details/currency": "USD"}, "Buyer Ltd", "Supplier Ltd"),
    ({"/charge_details/currency": "$"}, "Buyer Ltd", "Supplier Ltd"),
    ({"/financial_attributes/posting_type": "AR"}, "Buyer Ltd", "Supplier Ltd"),
    ({"/charge_details/currency": "USD"}, "Other Ltd", "Supplier Ltd"),
    ({"/charge_details/currency": "USD", "/document_info/document_date": "2026-99-99"}, "Buyer Ltd", "Supplier Ltd"),
    ({"/charge_details/currency": "USD", "/charge_details/quantity": "2", "/charge_details/unit_price": "30"}, "Buyer Ltd", "Supplier Ltd"),
])
def test_invalid_human_decision_cannot_mutate_ledger(web, tmp_path, fields, payer, payee):
    client, store, book = web
    result = finish(client, upload(client, book, eml(tmp_path / "a.eml", currency="$").read_bytes()))
    body = review_request(client, book, result, fields=fields)
    body.update(payer=payer, payee=payee)
    response = client.post(f"/api/jobs/{result['job']['id']}/review-preview", json=body)
    assert response.status_code == 400, response.text
    assert store.repository.snapshot(book["id"])[0].bills == []
    assert store.repository.get_decisions(result["job"]["id"]) == {}


def test_changed_preview_stale_ledger_and_missing_confirmation_are_rejected(web, tmp_path):
    client, store, book = web
    result = finish(client, upload(client, book, eml(tmp_path / "a.eml", currency="$").read_bytes()))
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    base = f"/api/jobs/{result['job']['id']}"
    token = client.post(base + "/review-preview", json=body).json()["preview_token"]
    missing = client.post(base + "/review-approve", json={**body, "preview_token": token})
    assert missing.json()["error"]["code"] == "REVIEW_CONFIRM_REQUIRED"
    changed = client.post(base + "/review-approve", json={**body, "reason": "changed", "confirmed": True, "preview_token": token})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "REVIEW_PREVIEW_CHANGED"
    state, rev = store.repository.snapshot(book["id"])
    store.repository.commit_job(job(store.repository, book, rev), state, [], [])
    stale = client.post(base + "/review-approve", json={**body, "confirmed": True, "preview_token": token})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "LEDGER_CHANGED"
    assert store.repository.snapshot(book["id"])[0].bills == []


def test_human_transaction_rolls_back_after_receipt_insert(web, tmp_path, monkeypatch):
    client, store, book = web
    result = finish(client, upload(client, book, eml(tmp_path / "a.eml", currency="$").read_bytes()))
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    base = f"/api/jobs/{result['job']['id']}"
    token = client.post(base + "/review-preview", json=body).json()["preview_token"]
    before = store.repository.snapshot(book["id"])

    def failure(*args):
        raise BillingError("TEST_RECEIPT_FAILURE", "Injected metadata failure")

    monkeypatch.setattr(store.repository, "_save_job", failure)
    response = client.post(base + "/review-approve", json={**body, "confirmed": True, "preview_token": token})
    assert response.status_code == 400
    assert store.repository.snapshot(book["id"]) == before
    assert store.repository.get_decisions(result["job"]["id"]) == {}


def test_old_pending_job_can_be_assigned_to_one_ledger_for_human_review(repository, tmp_path):
    file_store = JobStore(tmp_path, Settings(**ONLINE), pipeline=pipeline)
    item = file_store.submit([("old.eml", eml(tmp_path / "old.eml", currency="$").read_bytes())], entity="Buyer Ltd")
    file_store.close()
    book = repository.create_ledger("historical manual", "Buyer Ltd")
    with TestClient(create_app(tmp_path, Settings(**ONLINE), repository=repository),
                    base_url="http://127.0.0.1:8765") as client:
        client.headers["x-workspace-token"] = client.get("/api/config").json()["token"]
        result = client.get(f"/api/jobs/{item['id']}").json()
        body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
        approve(client, result, body)
        assert len(repository.snapshot(book["id"])[0].bills) == 1
        other = repository.create_ledger("wrong book", "Buyer Ltd")
        response = client.get(f"/api/jobs/{item['id']}/review-workspace",
                              params={"record_id": body["record_id"], "ledger_id": other["id"]})
        assert response.status_code == 409


def test_human_distinct_create_does_not_copy_old_erp_fields(web, tmp_path):
    client, store, book = web
    state = state_fixture()
    state.bills[0].document_info.reference_no = "INV-E2E-001"
    store.repository.import_state(book["id"], state, 0)
    result = finish(client, upload(client, book, eml(tmp_path / "distinct.eml", 70).read_bytes()))
    body = review_request(client, book, result)
    approve(client, result, body)
    accepted = store.repository.snapshot(book["id"])[0].bills
    assert len(accepted) == 2 and accepted[0].record_id == "OLD1"
    new = next(b.model_dump(mode="python", exclude_unset=True) for b in accepted if b.record_id != "OLD1")
    assert "erp_metadata" not in new and not new.get("extensions")
    assert new["charge_details"]["amount"] == 70


def test_human_confirmed_roles_not_overwritten_by_repeated_original_ocr(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "roles.eml", currency="$").read_bytes()
    result = finish(client, upload(client, book, raw))
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    body.update(payer="Supplier Ltd", payee="Buyer Ltd")
    receipt, _ = approve(client, result, body)
    assert receipt["after"]["financial_attributes"]["posting_type"] == "AR"
    repeated = finish(client, upload(client, book, raw))
    assert repeated["records"][0]["operation"] == "NO_CHANGE"
    assert store.repository.snapshot(book["id"])[0].bills[0].financial_attributes.posting_type == "AR"


def test_competing_human_approvals_have_one_durable_receipt(web, tmp_path):
    from shipping_billing.storage.review import ReviewRequest

    client, store, book = web
    result = finish(client, upload(client, book, eml(tmp_path / "concurrent.eml", currency="$").read_bytes()))
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    origin = store.review_origin(result["job"]["id"], body["record_id"], book["id"])
    request = ReviewRequest.model_validate(body)
    token = store.repository.preview_review(origin, request)["preview_token"]
    barrier = threading.Barrier(2)

    def commit(_):
        peer = LedgerRepository(store.repository.engine.url, allow_sqlite=True)
        try:
            if peer.backend == "mysql":
                barrier.wait(timeout=10)
            return peer.approve_review(origin, request, token)["decision_id"]
        finally:
            peer.close()

    if store.repository.backend == "mysql":
        with ThreadPoolExecutor(2) as executor:
            ids = list(executor.map(commit, range(2)))
    else:
        ids = [commit(0), commit(1)]
    assert ids[0] == ids[1]
    state, revision = store.repository.snapshot(book["id"])
    assert revision == body["revision"] + 1 and len(state.bills) == 1


def test_changed_observation_needs_review_again_and_cannot_overwrite_decision(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "original.eml", currency="$").read_bytes()
    result = finish(client, upload(client, book, raw))
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    receipt, token = approve(client, result, body)
    wrong = client.post(f"/api/jobs/{result['job']['id']}/review-approve", json={
        **body, "reason": "attempted overwrite", "preview_token": token, "confirmed": True})
    assert wrong.status_code == 409
    changed = finish(client, upload(client, book, eml(tmp_path / "changed.eml", 70, currency="$").read_bytes()))
    assert changed["records"][0]["operation"] == "PENDING_REVIEW"
    assert store.repository.snapshot(book["id"])[0].bills[0].charge_details.amount == 40
    assert store.repository.get_decisions(result["job"]["id"])[body["record_id"]]["decision_id"] == receipt["decision_id"]


def test_multiple_pending_fees_resolve_individually_without_marking_all_done(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "two.eml", rows=["Handling Fee | $ | 65 | PP", "Handling Fee | $ | 35 | PP"]).read_bytes()
    result = finish(client, upload(client, book, raw))
    assert len(result["records"]) == 2
    first = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    approve(client, result, first)
    current = client.get(f"/api/jobs/{result['job']['id']}").json()
    assert current["job"]["review_pending_count"] == 1
    assert len(current["review_decisions"]) == 1
    other = {**result, "records": [result["records"][1]]}
    second = review_request(client, book, other, fields={"/charge_details/currency": "USD"})
    approve(client, result, second)
    current = client.get(f"/api/jobs/{result['job']['id']}").json()
    assert current["job"]["review_pending_count"] == 0
    assert sorted(b.charge_details.amount for b in store.repository.snapshot(book["id"])[0].bills) == [35, 65]


def test_tampered_review_evidence_and_original_download_are_rejected(web, tmp_path):
    client, store, book = web
    raw = eml(tmp_path / "tamper.eml", currency="$").read_bytes()
    result = finish(client, upload(client, book, raw))
    body = review_request(client, book, result, fields={"/charge_details/currency": "USD"})
    source = result["files"][0]["source_id"]
    for path in (store.inputs / result["job"]["id"]).iterdir():
        path.write_bytes(b"tampered original")
    # An intact content-addressed archive is still a valid copy of the original.
    restored = client.get(f"/api/jobs/{result['job']['id']}/sources/{source}/download")
    assert restored.status_code == 200 and restored.content == raw
    from shipping_billing.web.service import native_path

    for path in native_path(store.folder(result["job"]["id"]) / "result/work/sources").glob("*"):
        if path.is_file():
            path.write_bytes(b"tampered archive")
    assert client.get(f"/api/jobs/{result['job']['id']}/sources/{source}/download").status_code == 404
    (store.folder(result["job"]["id"]) / "result/normalized.json").write_text("{}", encoding="utf-8")
    response = client.post(f"/api/jobs/{result['job']['id']}/review-preview", json=body)
    assert response.status_code == 400 and response.json()["error"]["code"] == "RESULT_INTEGRITY_FAILED"
    assert not store.repository.snapshot(book["id"])[0].bills
