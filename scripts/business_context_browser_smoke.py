"""Browser acceptance for scoped business context against an isolated SQL ledger."""

import socket
import threading
import time
from pathlib import Path
from uuid import uuid4

import uvicorn

from scripts.business_context_fixtures import business_email, business_pipeline
from shipping_billing.config import Settings
from shipping_billing.io import write_json
from shipping_billing.storage.database import LedgerRepository
from shipping_billing.web.server import create_app


def main():
    from playwright.sync_api import expect, sync_playwright

    output = Path("outputs/diagnostics/business-context-browser") / uuid4().hex
    output.mkdir(parents=True)
    mapping = output / "injected-ocr-map.json"
    write_json(mapping, {})
    repository = LedgerRepository("sqlite+pysqlite:///" + (output / "ledger.sqlite").resolve().as_posix(), allow_sqlite=True)
    app = create_app(output, Settings(mode="online", model_provider="test", model_name="injected QA",
                                     model_api_key="test-only"), repository=repository, pipeline=business_pipeline)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
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
            page = browser.new_page(viewport={"width": 1440, "height": 1050})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            expect(page.locator("#new-ledger-button")).to_be_visible()

            def new_book(name):
                page.locator("#new-ledger-button").click()
                page.locator("#new-ledger-name").fill(name)
                page.locator("#new-ledger-entity").fill("Buyer Ltd")
                page.locator("#create-ledger-submit").click()
                expect(page.locator("#create-ledger-dialog")).not_to_be_visible()
                return page.locator("#ledger-select").input_value()

            def upload(path):
                page.locator("#ocr-map").set_input_files(str(mapping.resolve()))
                page.locator("#document-input").set_input_files(str(path.resolve()))
                page.locator("#submit-button").click()
                expect(page.locator("#result-content")).to_be_visible(timeout=30000)
                expect(page.locator("#record-rows tr")).to_have_count(1)
                expect(page.locator("#business-links")).to_be_visible()

            first_book = new_book("Same invoice autofill")
            first_mail = business_email(output / "automatic", ambiguous_currency=False)
            page.locator("#document-input").set_input_files(str(first_mail.resolve()))
            before_jobs = len(repository.list_jobs())
            page.locator("#submit-button").click()
            expect(page.locator("#form-error")).to_contain_text("为 PDF / 图片启用指定 OCR")
            expect(page.locator("#ocr-map" if page.locator("#temporary-ocr").is_disabled()
                                else "#temporary-ocr")).to_be_focused()
            expect(page.locator("#selected-files")).to_contain_text(first_mail.name)
            assert len(repository.list_jobs()) == before_jobs
            assert repository.snapshot(first_book)[1] == 0
            page.screenshot(path=str(output / "missing-ocr-delivery.png"))
            checks.append("email_attachment_ocr_guard_keeps_files_and_does_not_create_job_or_ledger_revision")
            upload(first_mail)
            expect(page.locator("#record-rows .pill")).to_have_text("新增")
            expect(page.locator("#business-links")).to_contain_text("同一账单")
            page.locator("#record-rows button").click()
            expect(page.locator("#detail-body .related-field.applied").first).to_be_visible()
            page.locator("#detail-body .related-field.applied").first.scroll_into_view_if_needed()
            page.screenshot(path=str(output / "applied-evidence.png"))
            page.locator("#close-detail").click()
            page.locator("#continue-button").click()
            checks.append("same_invoice_header_autofill_with_evidence_and_single_fee")

            book_id = new_book("Related shipment suggestions")
            path = business_email(output / "suggestions", same_document=False)
            upload(path)
            expect(page.locator("#record-rows .pill")).to_have_text("待复核")
            expect(page.locator("#business-links")).to_contain_text("同票运输业务")
            page.locator(".review-record-button").click()
            expect(page.locator("#manual-preview-button")).to_be_enabled()
            expect(page.locator("#manual-field-currency")).to_have_value("")
            expect(page.locator("#manual-payer")).to_have_value("")
            page.locator("#manual-related-fields").scroll_into_view_if_needed()
            page.screenshot(path=str(output / "suggestions.png"))
            for text in ("确认付款方：Buyer Ltd", "确认收款方：Supplier Ltd", "签发方：Supplier Ltd", "接收方：Buyer Ltd", "币种：USD"):
                page.locator("#manual-related-fields .related-field").filter(has_text=text).get_by_role("button", name="采用此候选").click()
            expect(page.locator("#manual-payer")).to_have_value("Buyer Ltd")
            expect(page.locator("#manual-direction")).to_contain_text("应付 AP")
            expect(page.locator("#manual-field-currency")).to_have_value("USD")
            page.locator("#manual-reviewer").fill("关联业务验收员")
            page.locator("#manual-reason").fill("合成验收：核对相关原文后确认本笔费用的交易双方及币种。")
            page.locator("#manual-distinct").check()
            page.locator("#manual-preview-button").click()
            expect(page.locator("#manual-preview")).to_be_visible()
            expect(page.locator("#manual-approve")).to_be_disabled()
            page.locator("#manual-confirm").check()
            page.locator("#manual-approve").click()
            expect(page.locator("#manual-dialog")).not_to_be_visible(timeout=15000)
            expect(page.locator("#record-rows .pill")).to_have_text("新增")
            job_id = page.evaluate("S.jobId")
            state, revision = repository.snapshot(book_id)
            assert len(state.bills) == 1 and state.bills[0].charge_details.currency == "USD"
            receipt = next(iter(repository.get_decisions(job_id).values()))
            assert len(receipt["related_fields"]) == 5 and revision == 2
            page.reload()
            page.wait_for_function("() => S.config && S.ledgers.length > 0")
            page.evaluate("(id) => openJob(id)", job_id)
            expect(page.locator("#record-rows .pill")).to_have_text("新增")
            expect(page.locator("#result-meta")).to_contain_text("尚待复审 0 条")
            page.locator("#record-rows button").click()
            expect(page.locator("#detail-body")).to_contain_text("原始业务关联建议（已复核）")
            expect(page.locator("#detail-body")).not_to_contain_text("需确认的建议")
            page.locator("#close-detail").click()
            page.locator("#continue-button").click()
            upload(path)
            expect(page.locator("#record-rows .pill")).to_have_text("已入账，无需重复录入")
            assert len(repository.snapshot(book_id)[0].bills) == 1
            page.locator("#continue-button").click()
            checks.append("adopt_candidates_preview_confirm_sql_persistence_reload_and_repeat_deduplication")

            new_book("Conflicting sources")
            upload(business_email(output / "conflicts", conflict=True))
            expect(page.locator("#record-rows .pill")).to_have_text("待复核")
            page.locator(".review-record-button").click()
            expect(page.locator("#manual-preview-button")).to_be_enabled()
            expect(page.locator("#manual-related-fields .related-field.conflict").first).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#manual-related-fields .related-field.conflict").first.scroll_into_view_if_needed()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(output / "conflicts-mobile.png"))
            assert not errors, errors
            checks.append("conflicting_values_remain_pending_and_mobile_no_overflow")
            browser.close()
        write_json(output / "report.json", dict(passed=True, checks=checks, javascript_errors=errors,
                                                backend="isolated SQLite", external_model_calls=0, external_ocr_calls=0))
        print({"passed": True, "checks": len(checks), "artifacts": str(output)})
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
