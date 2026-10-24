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
