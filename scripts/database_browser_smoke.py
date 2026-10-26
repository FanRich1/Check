"""Edge acceptance against real MySQL with injected extraction and isolated local artifacts."""

import socket
import threading
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import uvicorn
from sqlalchemy.engine import make_url

from scripts.stage7_fixtures import AuthoredModel, eml
from shipping_billing.config import Settings
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.pipeline.engine import run_pipeline
from shipping_billing.web.server import create_app

ROOT = Path(__file__).resolve().parents[1]


def pipeline(paths, **kwargs):
    return run_pipeline(paths, **{**kwargs, "mode": "test_injected", "client": AuthoredModel()})


def first_use_checks(page, base, output, checks):
    from playwright.sync_api import expect

    fail_create = [True]

    def ledger_route(route):
        if route.request.method == "GET":
            route.fulfill(json={"ledgers": []})
        elif fail_create[0]:
            fail_create[0] = False
            route.fulfill(status=503, json={"error": {"message": "Test: database temporarily unavailable"}})
        else:
            route.continue_()

    page.route("**/api/ledgers", ledger_route)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base)
    expect(page.locator("#database-ledger-fields")).to_be_visible()
    expect(page.locator("#ledger-select")).to_be_disabled()
    expect(page.locator("#manage-ledger-button")).to_be_disabled()
    expect(page.locator("#ledger-next-step")).to_contain_text("首次使用")
    page.locator("#new-ledger-button").click()
    expect(page.locator("#new-ledger-entity")).to_be_focused()
    page.locator("#new-ledger-entity").fill("Buyer Ltd")
    expect(page.locator("#new-ledger-name")).to_have_value("")
    page.locator("#create-ledger-submit").click()
    expect(page.locator("#create-ledger-error")).to_contain_text("temporarily unavailable")
    expect(page.locator("#new-ledger-entity")).to_have_value("Buyer Ltd")
    expect(page.locator("#create-ledger-submit")).to_be_enabled()
    page.screenshot(path=str(output / "first-use-retry-mobile.png"), full_page=True)
    page.locator("#create-ledger-submit").click()
    expect(page.locator("#create-ledger-dialog")).not_to_be_visible()
    expect(page.locator("#ledger-select option:checked")).to_contain_text("Buyer Ltd · 业务账本")
    expect(page.locator("#ledger-next-step")).to_contain_text("下一步")
    expect(page.locator("#drop-zone")).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#manage-ledger-button").click()
    expect(page.locator("#ledger-bills")).to_contain_text("账本已就绪")
    page.locator("#return-to-upload").click()
    expect(page.locator("#ledger-dialog")).not_to_be_visible()
    expect(page.locator("#drop-zone")).to_be_focused()
    page.screenshot(path=str(output / "first-use-ready-mobile.png"), full_page=True)
    checks.append("first_use_empty_books_optional_name_create_error_retry_mobile_return_to_upload")


def manual_checks(page, output, checks):
    from playwright.sync_api import expect

    page.locator("#new-ledger-button").click()
    page.locator("#new-ledger-name").fill("人工复审验收 " + uuid4().hex[:8])
    page.locator("#new-ledger-entity").fill("Buyer Ltd")
    page.locator('#create-ledger-form button[type="submit"]').click()
    expect(page.locator("#create-ledger-dialog")).not_to_be_visible()
    expect(page.locator("#entity")).to_have_value("Buyer Ltd")

    def upload_pending(path, *, use_callout=False):
        page.locator("#document-input").set_input_files(str(path))
        page.locator("#submit-button").click()
        expect(page.locator("#result-content")).to_be_visible(timeout=30000)
        expect(page.locator("#record-rows .pill")).to_have_text("待复核")
        expect(page.locator("#pending-review-callout")).to_be_visible()
        expect(page.locator("#pending-review-summary")).to_contain_text("1 条费用待复核")
        expect(page.locator(".review-record-button")).to_have_count(1)
        page.locator("#start-pending-review" if use_callout else ".review-record-button").click()
        expect(page.locator("#detail-dialog")).not_to_be_visible()
        expect(page.locator("#manual-preview-button")).to_be_enabled()
        page.locator("#manual-reviewer").fill("浏览器复审员")
        page.locator("#manual-reason").fill("对照原件核对交易双方、金额和币种，确认本次决定。")

    def submit(expected):
        page.locator("#manual-preview-button").click()
        expect(page.locator("#manual-preview")).to_be_visible()
        expect(page.locator("#manual-approve")).to_be_disabled()
        page.locator("#manual-confirm").check()
        page.locator("#manual-approve").click()
        expect(page.locator("#manual-dialog")).not_to_be_visible(timeout=15000)
        expect(page.locator("#record-rows .pill")).to_have_text(expected)
        expect(page.locator("#result-meta")).to_contain_text("尚待复审 0 条")
        expect(page.locator("#pending-review-callout")).not_to_be_visible()
        expect(page.locator(".review-record-button")).to_have_count(0)
        expect(page.locator("#source-files .pill")).to_have_text("费用已复核")
        expect(page.locator(".source-review-summary")).to_contain_text("剩余 0 条")
        expect(page.locator("#source-files .source-history summary")).to_contain_text("原始识别提示")
        expect(page.locator("#processing-summary")).to_contain_text("0 条待复核")

    path = eml(output / "manual-new.eml", currency="$")
    upload_pending(path, use_callout=True)
    page.locator("#manual-field-currency").fill("USD")
    page.locator("#manual-payer").fill("Buyer Ltd.")
    expect(page.locator("#manual-direction")).to_contain_text("应付 AP")
    page.locator("#manual-distinct").check()
    submit("新增")
    page.locator("#download-kind").select_option("review-decisions.json")
    with page.expect_download() as download:
        page.locator("#download-button").click()
    download.value.save_as(output / "manual-decisions.json")
    assert next(iter(load_json(output / "manual-decisions.json").values()))["action"] == "create"
    page.screenshot(path=str(output / "manual-created.png"), full_page=True)
    page.locator("#continue-button").click()
    page.locator("#document-input").set_input_files(str(path))
    page.locator("#submit-button").click()
    expect(page.locator("#result-content")).to_be_visible(timeout=30000)
    expect(page.locator("#record-rows .pill")).to_have_text("已入账，无需重复录入")
    expect(page.locator("#record-rows .operation-explanation")).to_contain_text("已匹配当前账本")
    page.locator("#continue-button").click()
    checks.append("human_direct_review_callout_correct_currency_suffix_direction_receipt_download_exact_source_repeat")

    upload_pending(eml(output / "manual-update.eml", 70))
    expect(page.locator("#manual-action")).to_have_value("update")
    expect(page.locator("#manual-field-amount")).to_have_value("70")
    page.locator("#manual-field-amount").fill("72.50")
    page.locator("#manual-preview-button").click()
    expect(page.locator("#manual-changes")).to_contain_text("40 → 72.50")
    page.screenshot(path=str(output / "manual-update-preview.png"), full_page=True)
    page.locator("#manual-confirm").check()
    page.locator("#manual-approve").click()
    expect(page.locator("#manual-dialog")).not_to_be_visible(timeout=15000)
    expect(page.locator("#record-rows .pill")).to_have_text("修改")
    expect(page.locator("#record-rows .amount")).to_have_text("72.50")
    page.locator("#continue-button").click()
    checks.append("human_update_preview_explicit_confirmation_existing_bill_retained")

    upload_pending(eml(output / "manual-duplicate.eml", 90))
    page.locator("#manual-action").select_option("duplicate")
    submit("已入账，无需重复录入")
    expect(page.locator("#record-rows .operation-explanation")).to_contain_text("人工复核已确认重复")
    expect(page.locator("#record-rows .amount")).to_have_text("72.50")
    page.locator("#continue-button").click()
    checks.append("human_duplicate_preserves_old_amount")

    rejected = eml(output / "manual-reject.eml", currency="$")
    rejected.write_bytes(rejected.read_bytes().replace(b"INV-E2E-001", b"INV-MANUAL-REJECT"))
    upload_pending(rejected)
    page.locator("#manual-action").select_option("reject")
    submit("已拒绝")
    page.locator("#continue-button").click()
    checks.append("human_reject_no_financial_booking")

    large = eml(output / "manual-large.eml", amount="9007199254740993.12", currency="$")
    large.write_bytes(large.read_bytes().replace(b"INV-E2E-001", b"INV-MANUAL-LARGE"))
    upload_pending(large)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#manual-dialog").evaluate("e => e.scrollWidth <= e.clientWidth")
    page.locator("#manual-dialog").evaluate("e => {e.scrollTop = 0}")
    page.screenshot(path=str(output / "manual-mobile.png"), full_page=True)
    expect(page.locator("#manual-field-amount")).to_have_value("9007199254740993.12")
    page.locator("#manual-field-currency").fill("USD")
    page.locator("#manual-distinct").check()
    submit("新增")
    expect(page.locator("#record-rows .amount")).to_have_text("9007199254740993.12")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    with page.expect_download() as download:
        page.locator("#download-current-ledger").click()
    download.value.save_as(output / "manual-final-ledger.json")
    exported = load_json(output / "manual-final-ledger.json")
    assert len(exported["bills"]) == 2
    assert sorted(b["charge_details"]["amount"] for b in exported["bills"]) == [Decimal("72.50"), Decimal("9007199254740993.12")]
    page.reload()
    page.locator("#nav-history").click()
    page.locator("#history-jobs .job-card").first.click()
    expect(page.locator("#record-rows .amount")).to_have_text("9007199254740993.12")
    page.locator("#continue-button").click()
    checks.append("human_large_decimal_refresh_persistence")
    checks.append("human_mobile_form_and_direct_final_ledger_export")


def main():
    from playwright.sync_api import expect, sync_playwright

    settings = Settings(_env_file=ROOT / ".env")
    url = make_url(settings.database_url.get_secret_value()).set(database="shipping_billing_test")
    output = ROOT / "outputs/mysql/browser" / uuid4().hex
    output.mkdir(parents=True)
    app = create_app(output, Settings(mode="online", model_provider="test", model_name="injected QA",
                                     model_api_key="test-only", database_url=url.render_as_string(hide_password=False)),
                     pipeline=pipeline)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    errors, checks = [], []
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.1)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            first_page = browser.new_page()
            first_page.on("pageerror", lambda error: errors.append(str(error)))
            first_use_checks(first_page, f"http://127.0.0.1:{port}", output, checks)
            first_page.close()
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#database-ledger-fields")).to_be_visible()
            expect(page.locator("#legacy-ledger-input")).to_be_hidden()
            expect(page.locator("#ledger-select")).to_have_value("")
            expect(page.locator("#entity")).to_be_editable()
            page.locator("#entity").fill("Buyer Ltd")
            page.reload()
            expect(page.locator("#database-ledger-fields")).to_be_visible()
            expect(page.locator("#entity")).to_be_editable()
            expect(page.locator("#entity")).to_have_value("Buyer Ltd")
            raw = eml(output / "new.eml")
            page.locator("#document-input").set_input_files(str(raw))
            page.locator("#submit-button").click()
            expect(page.locator("#create-ledger-dialog")).to_be_visible()
            expect(page.locator("#ledger-dialog")).not_to_be_visible()
            expect(page.locator("#selected-files")).to_contain_text(raw.name)
            expect(page.locator("#new-ledger-entity")).to_have_value("Buyer Ltd")
            page.locator("#new-ledger-name").fill("浏览器验收 " + uuid4().hex[:8])
            page.locator('#create-ledger-form button[type="submit"]').click()
            expect(page.locator("#create-ledger-dialog")).not_to_be_visible()
            expect(page.locator("#ledger-dialog")).not_to_be_visible()
            expect(page.locator("#form-error")).to_be_hidden()
            expect(page.locator("#selected-files")).to_contain_text(raw.name)
            expect(page.locator("#submit-button")).to_be_focused()
            expect(page.locator("#entity")).to_have_value("Buyer Ltd")
            assert page.locator("#entity").evaluate("e => e.readOnly")
            expect(page.locator("#entity-help")).to_contain_text("记账主体由所选账本确定")
            selected = page.locator("#ledger-select").input_value()
            page.locator("#ledger-select").select_option("")
            expect(page.locator("#entity")).to_be_editable()
            page.locator("#entity").fill("Another Company")
            page.locator("#ledger-select").select_option(selected)
            expect(page.locator("#entity")).to_have_value("Buyer Ltd")
            assert page.locator("#entity").evaluate("e => e.readOnly")
            page.locator("#ledger-select").select_option("")
            expect(page.locator("#entity")).to_have_value("Another Company")
            page.locator("#new-ledger-button").click()
            expect(page.locator("#new-ledger-entity")).to_have_value("Another Company")
            page.locator("#cancel-create-ledger").click()
            page.reload()
            expect(page.locator("#entity")).to_be_editable()
            expect(page.locator("#entity")).to_have_value("Another Company")
            expect(page.locator("#ledger-select")).to_have_value("")
            page.locator("#ledger-select").select_option(selected)
            page.screenshot(path=str(output / "workspace.png"), full_page=True)
            checks.append("entity_editable_without_ledger_draft_persists_new_ledger_prefills_bound_entity_locked")
            checks.append("missing_ledger_create_returns_to_upload_keeps_files_clears_error")
            checks.append("create_select_company_isolation")

            def process(path, expected):
                page.locator("#document-input").set_input_files(str(path))
                page.locator("#submit-button").click()
                expect(page.locator("#result-content")).to_be_visible(timeout=30000)
                expect(page.locator("#record-rows tr")).to_have_count(1)
                expect(page.locator("#record-rows .pill")).to_contain_text(expected)
                expect(page.locator("#result-meta")).to_contain_text("共 1 条有效费用")

            raw = eml(output / "new.eml")
            process(raw, "新增")
            page.locator("#continue-button").click()
            process(raw, "已入账，无需重复录入")
            page.locator("#continue-button").click()
            process(eml(output / "amend.eml", 50, note="Update amount from 40 to 50"), "修改")
            expect(page.locator("#record-rows .amount")).to_have_text("50")
            checks.append("upload_repeat_authorized_update_automatic_ledger_matching")
            page.screenshot(path=str(output / "updated.png"), full_page=True)
            page.locator("#continue-button").click()
            page.locator("#manage-ledger-button").click()
            expect(page.locator("#ledger-bills")).to_contain_text("50 USD")
            expect(page.locator("#create-ledger-form")).not_to_be_visible()
            page.locator("#ledger-import-options summary").click()
            expect(page.locator("#import-ledger-button")).to_be_disabled()
            with page.expect_download() as download:
                page.locator("#export-ledger-button").click()
            download.value.save_as(output / "exported.json")
            assert len(load_json(output / "exported.json")["bills"]) == 1
            assert load_json(output / "exported.json")["bills"][0]["charge_details"]["amount"] == 50
            page.locator("#close-ledger").click()
            selected = page.locator("#ledger-select").input_value()
            page.reload()
            expect(page.locator("#ledger-select")).to_have_value(selected)
            expect(page.locator("#ledger-summary")).to_contain_text("1 条有效费用")
            checks.append("export_refresh_persistence_import_overwrite_disabled")
            page.locator("#new-ledger-button").click()
            page.locator("#new-ledger-name").fill("旧账导入验收 " + uuid4().hex[:8])
            page.locator("#new-ledger-entity").fill("Buyer Ltd")
            page.locator('#create-ledger-form button[type="submit"]').click()
            expect(page.locator("#create-ledger-dialog")).not_to_be_visible()
            page.locator("#manage-ledger-button").click()
            expect(page.locator("#managed-ledger-summary")).to_contain_text("版本 0")
            page.locator("#ledger-import-options summary").click()
            expect(page.locator("#import-ledger-button")).to_be_disabled()
            expect(page.locator("#ledger-import-help")).to_contain_text("没有旧账单可直接返回")
            imported = load_json(ROOT / "tests/fixtures/reconciliation/example-old-state.json")
            imported["bills"][0]["charge_details"]["amount"] = Decimal("9007199254740993.12")
            old_path = output / "import.json"
            write_json(old_path, imported)
            page.locator("#import-ledger-file").set_input_files(str(old_path))
            expect(page.locator("#import-ledger-button")).to_be_enabled()
            page.locator("#import-ledger-button").click()
            expect(page.locator("#ledger-bills")).to_contain_text("9007199254740993.12 USD")
            with page.expect_download() as download:
                page.locator("#export-ledger-button").click()
            download.value.save_as(output / "large-export.json")
            assert dumps(load_json(output / "large-export.json")) == dumps(imported)
            page.screenshot(path=str(output / "ledger-import.png"), full_page=True)
            page.locator("#close-ledger").click()
            checks.append("ledger_management_optional_import_disabled_until_file_selected")
            checks.append("import_old_ledger_large_decimal_extensions_exact_download")
            manual_checks(page, output, checks)
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path=str(output / "mobile.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#manage-ledger-button").click()
            expect(page.locator("#ledger-dialog")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            checks.append("mobile_390px_no_document_overflow")
            assert not errors, errors
            browser.close()
        write_json(output / "report.json", dict(passed=True, backend="mysql", checks=checks,
                                                javascript_errors=errors, external_model_calls=0, external_ocr_calls=0))
        print({"passed": True, "checks": len(checks), "artifacts": str(output)})
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=15)


if __name__ == "__main__":
    raise SystemExit(main())
