"""Exercise the browser API against real raw documents and the existing pipeline rules."""

import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scripts.stage7_fixtures import AuthoredModel, eml
from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.web.server import create_app
from shipping_billing.web.service import JobStore, native_path

ROOT = Path(__file__).resolve().parents[2]
ONLINE = dict(
    mode="online", model_provider="deepseek", model_name="test", model_api_key="test-only-not-a-key"
)


def injected_pipeline(paths, **kwargs):
    kwargs.update(mode="test_injected", client=AuthoredModel())
    return run_pipeline(paths, **kwargs)


@pytest.fixture
def workspace(tmp_path):
    app = create_app(tmp_path, Settings(**ONLINE), pipeline=injected_pipeline)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/config").json()["token"]
        client.headers["x-workspace-token"] = token
        yield client, app.state.store


def upload(client, raw, *, extra=None, data=None, filename="invoice.eml"):
    files = [("documents", (filename, raw, "application/octet-stream")), *(extra or [])]
    return client.post("/api/jobs", data={"entity": "Buyer Ltd", **(data or {})}, files=files)


def finished(client, response):
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    for _ in range(150):
        response = client.get("/api/jobs/" + job_id)
        assert response.status_code == 200, response.text
        if response.json()["job"]["status"] not in ("queued", "running"):
            return response.json()
        time.sleep(0.02)
    raise AssertionError("Job did not complete")


def test_live_upload_create_repeat_and_exact_download(workspace, tmp_path):
    client, store = workspace
    raw = eml(tmp_path / "new.eml").read_bytes()
    first = finished(client, upload(client, raw))
    job_id = first["job"]["id"]
    assert first["job"]["status"] == "completed"
    assert first["records"][0]["operation"] == "CREATE"
    assert first["records"][0]["amount_display"] == "40"
    assert first["records"][0]["audit_trail"]["field_evidence"]
    response = client.get(f"/api/jobs/{job_id}/download/records.json")
    assert response.status_code == 200
    assert response.content == (store.folder(job_id) / "result/records.json").read_bytes()
    second = finished(client, upload(client, raw, data={"base_job": job_id}))
    assert second["records"][0]["operation"] == "NO_CHANGE"
    assert len(load_json(store.folder(second["job"]["id"]) / "result/state.json")["bills"]) == 1
    assert len(client.get("/api/jobs").json()["jobs"]) == 2


def test_uploaded_ledger_and_authorized_update_preserve_extensions(workspace, tmp_path):
    client, store = workspace
    first = finished(client, upload(client, eml(tmp_path / "new.eml").read_bytes()))
    state = load_json(store.folder(first["job"]["id"]) / "result/state.json")
    state["bills"][0]["extensions"] = {"retain": {"list": [1, True, "x"]}}
    raw = eml(tmp_path / "amendment.eml", 50, note="Update amount from 40 to 50").read_bytes()
    second = finished(
        client, upload(client, raw, extra=[("old_state", ("state.json", dumps(state), "application/json"))])
    )
    assert second["records"][0]["operation"] == "UPDATE"
    assert second["records"][0]["changes_display"][0]["old"] == "40"
    saved = load_json(store.folder(second["job"]["id"]) / "result/state.json")
    assert saved["bills"][0]["extensions"] == {"retain": {"list": [1, True, "x"]}}


def test_partial_source_failure_and_review_do_not_change_ledger(workspace, tmp_path):
    client, store = workspace
    response = upload(
        client,
        eml(tmp_path / "new.eml").read_bytes(),
        extra=[("documents", ("broken.docx", b"broken document", "application/octet-stream"))],
    )
    result = finished(client, response)
    assert result["job"]["status"] == "error" and len(result["records"]) == 1
    assert result["processing_summary"]["fee_lines"] == 1
    assert result["processing_summary"]["source_errors"] == 1
    assert result["processing_summary"]["attachment_errors"] == 0
    assert any(f["status"] == "error" for f in result["files"])
    folder = store.folder(result["job"]["id"]) / "result"
    before = {name: (folder / name).read_bytes() for name in ("state.json", "records.json", "audit.json")}
    response = client.post(
        f"/api/jobs/{result['job']['id']}/review",
        json={
            "record_id": result["records"][0]["record_id"],
            "status": "checked",
            "note": "Checked original document",
        },
    )
    assert response.status_code == 200
    assert all((folder / name).read_bytes() == data for name, data in before.items())
    downloaded = client.get(f"/api/jobs/{result['job']['id']}/download/reviews.json")
    assert downloaded.status_code == 200 and b"Checked original document" in downloaded.content


def test_pending_amount_does_not_become_accepted_when_marked_checked(workspace, tmp_path):
    client, store = workspace
    raw = eml(tmp_path / "ambiguous.eml", currency="$").read_bytes()
    result = finished(client, upload(client, raw))
    assert result["job"]["status"] == "pending_review"
    record = result["records"][0]
    assert record["operation"] == "PENDING_REVIEW" and record["charge_details"]["currency"] is None
    assert any(i["code"] == "AMBIGUOUS_CURRENCY" and i["explanation"] for i in record["issues"])
    client.post(
        f"/api/jobs/{result['job']['id']}/review",
        json={"record_id": record["record_id"], "status": "checked", "note": "looked"},
    )
    after = client.get(f"/api/jobs/{result['job']['id']}").json()
    assert after["records"][0]["operation"] == "PENDING_REVIEW"
    assert load_json(store.folder(result["job"]["id"]) / "result/state.json")["bills"] == []


def test_large_amount_display_and_download_preserve_decimal(workspace, tmp_path):
    client, store = workspace
    raw = eml(tmp_path / "large.eml", amount="9007199254740993.12").read_bytes()
    result = finished(client, upload(client, raw))
    assert result["records"][0]["amount_display"] == "9007199254740993.12"
    response = client.get(f"/api/jobs/{result['job']['id']}/download/records.json")
    assert b"9007199254740993.12" in response.content


@pytest.mark.parametrize(
    "invalid", ["null", "false", "0", '""', '{"bills":[],"bills":[]}', '{"bad":true}', "[{}]"]
)
def test_invalid_old_state_is_rejected_before_job_creation(workspace, tmp_path, invalid):
    client, store = workspace
    response = upload(
        client,
        eml(tmp_path / "new.eml").read_bytes(),
        extra=[("old_state", ("old.json", invalid, "application/json"))],
    )
    assert response.status_code == 400, response.text
    assert client.get("/api/jobs").json()["jobs"] == []
    assert not store.active


def test_source_filenames_cannot_escape_upload_folder(workspace, tmp_path):
    client, store = workspace
    raw = eml(tmp_path / "new.eml").read_bytes()
    response = upload(client, raw, filename="../../escape.eml")
    result = finished(client, response)
    saved = list((store.inputs / result["job"]["id"]).iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == raw
    assert saved[0].resolve().is_relative_to(store.inputs.resolve())


def test_cross_origin_missing_token_and_arbitrary_downloads_are_rejected(workspace, tmp_path):
    client, _ = workspace
    token = client.headers.pop("x-workspace-token")
    assert client.post("/api/demo").status_code == 403
    client.headers["x-workspace-token"] = token
    assert client.post("/api/demo", headers={"origin": "https://untrusted.example"}).status_code == 403
    assert client.get("/api/jobs", headers={"sec-fetch-site": "cross-site"}).status_code == 403
    # Following the workspace link may navigate from another site; private APIs stay same-origin only.
    assert (
        client.get(
            "/",
            headers={
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
            },
        ).status_code
        == 200
    )
    assert client.get("/api/config", headers={"host": "untrusted.example"}).status_code == 400
    result = finished(client, upload(client, eml(tmp_path / "new.eml").read_bytes()))
    assert client.get(f"/api/jobs/{result['job']['id']}/download/.env").status_code == 404
    assert client.get("/api/jobs/not-a-job").status_code == 404
    assert client.get("/assets/private.json").status_code == 404
    assert "test-only-not-a-key" not in client.get("/api/config").text
    page = client.get("/")
    assert page.status_code == 200 and "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_tampered_artifact_cannot_be_viewed_downloaded_or_continued(workspace, tmp_path):
    client, store = workspace
    raw = eml(tmp_path / "new.eml").read_bytes()
    result = finished(client, upload(client, raw))
    job_id = result["job"]["id"]
    path = store.folder(job_id) / "result/state.json"
    path.write_text('{"bills":[]}', encoding="utf-8")
    assert client.get(f"/api/jobs/{job_id}").status_code == 400
    assert client.get(f"/api/jobs/{job_id}/download/records.json").status_code == 400
    assert upload(client, raw, data={"base_job": job_id}).status_code == 400


def test_invalid_input_size_type_and_ocr_preconditions(workspace, tmp_path):
    client, _ = workspace
    assert upload(client, b"").status_code == 400
    assert upload(client, b"not executable", filename="file.exe").status_code == 400
    assert upload(client, b"pdf", filename="bill.pdf").json()["error"]["code"] == "MISSING_OCR_DELIVERY"
    assert (
        upload(client, b"png", filename="bill.png", data={"temporary_ocr": "true"}).json()["error"]["code"]
        == "TUNNEL_TOOL_MISSING"
    )
    assert client.post("/api/demo", headers={"content-length": "999999999"}).status_code == 413
    response = upload(client, eml(tmp_path / "new.eml").read_bytes(), data={"entity": ""})
    assert response.status_code == 400


def test_explicit_ocr_delivery_passes_only_to_backend(tmp_path):
    executable = tmp_path / "cloudflared.exe"
    executable.write_bytes(b"test marker, never executed")
    observed = []

    def pipeline(paths, **kwargs):
        observed.append(kwargs["cloudflared"])
        kwargs.pop("cloudflared")
        return injected_pipeline(paths, **kwargs)

    with TestClient(
        create_app(tmp_path, Settings(**ONLINE), cloudflared=executable, pipeline=pipeline),
        base_url="http://127.0.0.1:8765",
    ) as client:
        client.headers["x-workspace-token"] = client.get("/api/config").json()["token"]
        finished(
            client, upload(client, eml(tmp_path / "new.eml").read_bytes(), data={"temporary_ocr": "true"})
        )
    assert observed == [executable]


def test_demo_parses_real_public_workbook_using_archives(tmp_path):
    fixture = Path("tests/fixtures/harness")
    for relative in (fixture / "DEV-025", fixture / "replay-cache/development/round-1"):
        shutil.copytree(native_path(ROOT / relative), native_path(tmp_path / relative))
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8765") as client:
        client.headers["x-workspace-token"] = client.get("/api/config").json()["token"]
        result = finished(client, client.post("/api/demo"))
        assert result["job"]["demo"] and len(result["records"]) == 2
        assert [r["amount_display"] for r in result["records"]] == ["21", "31"]
        manifest = client.get(f"/api/jobs/{result['job']['id']}/download/manifest.json").json()
        assert manifest["mode"] == "replay" and manifest["model_invocations"] == 0


def test_restart_preserves_history_reviews_and_marks_unfinished_tasks(workspace, tmp_path):
    client, store = workspace
    result = finished(client, upload(client, eml(tmp_path / "new.eml").read_bytes()))
    job_id = result["job"]["id"]
    client.post(
        f"/api/jobs/{job_id}/review",
        json={"record_id": result["records"][0]["record_id"], "status": "checked", "note": "persistent"},
    )
    store.update(job_id, status="running")
    second = JobStore(tmp_path, Settings())
    try:
        assert second.get(job_id)["status"] == "interrupted"
        assert second.result(job_id)["reviews"][result["records"][0]["record_id"]]["note"] == "persistent"
    finally:
        second.close()


def test_queue_limit_does_not_create_extra_jobs(tmp_path):
    release = threading.Event()

    def blocking_pipeline(*args, **kwargs):
        release.wait(5)
        raise BillingError("TEST_STOP", "stopped")

    store = JobStore(tmp_path, Settings(**ONLINE), pipeline=blocking_pipeline)
    try:
        raw = eml(tmp_path / "new.eml").read_bytes()
        for _ in range(3):
            store.submit([("new.eml", raw)], entity="Buyer Ltd")
        with pytest.raises(BillingError) as exc:
            store.submit([("new.eml", raw)], entity="Buyer Ltd")
        assert exc.value.code == "QUEUE_FULL" and len(store.listing()) == 3
    finally:
        release.set()
        store.close()


def test_local_ocr_is_explicit_and_required_dependency_checked(workspace, tmp_path, monkeypatch):
    client, store = workspace
    monkeypatch.setattr("shipping_billing.web.service.local_ocr_available", lambda: False)
    before = len(store.listing())
    response = upload(
        client, eml(tmp_path / "new.eml").read_bytes(), data={"local_ocr_enabled": "true"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "LOCAL_OCR_NOT_INSTALLED"
    assert len(store.listing()) == before


@pytest.mark.parametrize("email_attachment", [False, True])
def test_images_without_local_text_are_rejected_before_creating_a_job(workspace, tmp_path, email_attachment):
    import io

    from PIL import Image

    client, store = workspace
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image, format="PNG")
    path = tmp_path / "bill.png"
    path.write_bytes(image.getvalue())
    before = len(store.listing())
    raw = eml(tmp_path / "image.eml", attachment=path).read_bytes() if email_attachment else path.read_bytes()
    response = upload(client, raw, filename="image.eml" if email_attachment else "bill.png",
                      extra=[("ocr_map", ("map.json", "{}", "application/json"))])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "LOCAL_OCR_REQUIRED"
    assert len(store.listing()) == before


def test_email_pdf_attachments_need_ocr_delivery_before_creating_a_job(workspace, tmp_path, monkeypatch):
    from scripts.business_context_fixtures import business_email

    client, store = workspace
    def must_not_run(*args, **kwargs):
        raise AssertionError("No model or OCR work may run before prerequisite validation")
    store.pipeline = must_not_run
    monkeypatch.setattr("shipping_billing.web.service.local_ocr_available", lambda: True)
    before = len(store.listing())
    response = upload(client, business_email(tmp_path / "mail").read_bytes(),
                      data={"local_ocr_enabled": "true"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "MISSING_OCR_DELIVERY"
    assert "为 PDF / 图片启用指定 OCR" in error["message"]
    assert len(store.listing()) == before
    assert not store.inputs.exists() or not list(store.inputs.iterdir())


def test_source_purpose_confirmation_works_in_file_mode_and_preserves_raw_failure(workspace, tmp_path, monkeypatch):
    from scripts.source_usage_fixtures import DummyLocalOCR, source_email, source_pipeline

    client, store = workspace
    monkeypatch.setattr("shipping_billing.web.service.local_ocr_available", lambda: True)
    monkeypatch.setattr("shipping_billing.parsers.local_text.LocalTextOCR", DummyLocalOCR)
    store.pipeline = source_pipeline
    result = finished(client, upload(client, source_email(tmp_path / "materials").read_bytes(),
        data={"local_ocr_enabled": "true"}, extra=[("ocr_map", ("map.json", "{}", "application/json"))]))
    image = result["files"][1]
    assert image["assessment"]["status"] == "purpose_pending"
    job_id = result["job"]["id"]
    body = dict(source_id=image["source_id"], sha256=image["sha256"], revision=0,
                classification="non_billing_image", reviewer="File reviewer", reason="Viewed original banner", confirmed=True)
    response = client.post(f"/api/jobs/{job_id}/source-review", json=body)
    assert response.status_code == 200, response.text
    current = client.get(f"/api/jobs/{job_id}").json()
    assert current["job"]["status"] == "completed" and current["job"]["original_status"] == "pending_review"
    assert current["files"][1]["status"] == "pending_review"
    assert current["files"][1]["assessment"]["status"] == "non_billing"
    assert len(load_json(store.folder(job_id) / "source-reviews.json")[image["source_id"]]) == 1
    assert client.get(f"/api/jobs/{job_id}/download/source-reviews.json").status_code == 200


def test_source_purpose_confirmation_cannot_hide_unreadable_files(workspace, tmp_path):
    client, _ = workspace
    attachment = tmp_path / "broken.docx"
    attachment.write_bytes(b"not an Office file")
    result = finished(client, upload(client, eml(tmp_path / "mail.eml", attachment=attachment).read_bytes()))
    source = result["files"][1]
    assert source["assessment"]["status"] == "error" and not source["assessment"]["can_confirm"]
    response = client.post(f"/api/jobs/{result['job']['id']}/source-review", json=dict(
        source_id=source["source_id"], sha256=source["sha256"], revision=0, classification="supporting_document",
        reviewer="Reviewer", reason="Trying to hide a read failure", confirmed=True))
    assert response.status_code == 400 and response.json()["error"]["code"] == "SOURCE_REVIEW_UNAVAILABLE"


def test_local_ocr_option_reaches_pipeline_and_is_saved(workspace, tmp_path, monkeypatch):
    client, store = workspace
    marker = object()
    monkeypatch.setattr("shipping_billing.web.service.local_ocr_available", lambda: True)
    monkeypatch.setattr("shipping_billing.parsers.local_text.LocalTextOCR", lambda: marker)
    captured = []

    def pipeline(paths, **kwargs):
        captured.append(kwargs.pop("local_ocr"))
        return injected_pipeline(paths, **kwargs)

    store.pipeline = pipeline
    result = finished(
        client,
        upload(client, eml(tmp_path / "new.eml").read_bytes(), data={"local_ocr_enabled": "true"}),
    )
    assert captured == [marker]
    assert result["job"]["local_ocr_enabled"] is True
    assert result["records"][0]["operation"] == "CREATE"
