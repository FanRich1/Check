"""Source-purpose browser acceptance on synthetic inputs and isolated SQLite."""

import hashlib
import socket
import threading
import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import uvicorn

from scripts.source_usage_fixtures import DummyLocalOCR, source_email, source_pipeline
from shipping_billing.config import Settings
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.storage.database import LedgerRepository
from shipping_billing.web.server import create_app


def main():
    from playwright.sync_api import expect, sync_playwright

    output = Path("outputs/diagnostics/source-usage-browser") / uuid4().hex
    output.mkdir(parents=True)
    mapping = output / "injected-ocr-map.json"
    write_json(mapping, {})
    email = source_email(output / "inputs")
    repository = LedgerRepository("sqlite+pysqlite:///" + (output / "ledger.sqlite").resolve().as_posix(), allow_sqlite=True)
    app = create_app(output, Settings(mode="online", model_provider="test", model_name="injected QA",
                                     model_api_key="test-only"), repository=repository, pipeline=source_pipeline)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    errors, checks = [], []
    try:
        with patch("shipping_billing.web.service.local_ocr_available", return_value=True), \
                patch("shipping_billing.web.server.local_ocr_available", return_value=True), \
                patch("shipping_billing.parsers.local_text.LocalTextOCR", DummyLocalOCR):
            thread.start()
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
                page.locator("#new-ledger-button").click()
                page.locator("#new-ledger-name").fill("Source purpose acceptance")
                page.locator("#new-ledger-entity").fill("Buyer Ltd")
                page.locator("#create-ledger-submit").click()
                expect(page.locator("#create-ledger-dialog")).not_to_be_visible()
                book = page.locator("#ledger-select").input_value()
                page.locator("#ocr-map").set_input_files(str(mapping.resolve()))
                page.locator("#local-ocr").check()
                page.locator("#document-input").set_input_files(str(email.resolve()))
                page.locator("#submit-button").click()
                expect(page.locator("#result-content")).to_be_visible(timeout=30000)
                expect(page.locator("#record-rows tr")).to_have_count(1)
                expect(page.locator("#source-files .source-row")).to_have_count(4)
                expect(page.locator("#source-files .pill").filter(has_text="已读取·运输支持材料")).to_have_count(2)
                expect(page.locator("#processing-summary")).to_contain_text("0 个异常")
                expect(page.locator("#processing-summary")).to_contain_text("1 个用途待确认")
                expect(page.locator("#result-banner")).to_have_class("notice warning")
                image = page.locator("#source-files .source-row").filter(has_text="banner.png")
                expect(image.locator(".pill")).to_have_text("用途待确认")
                image.scroll_into_view_if_needed()
                page.screenshot(path=str(output / "purpose-pending.png"))
                job = page.evaluate("S.jobId")
                before, revision = repository.snapshot(book)
                frozen = {path.name: path.read_bytes() for path in (app.state.store.folder(job) / "result").glob("*.json")}
                checks.append("transport_native_evidence_and_unknown_image_separate_from_fees_and_errors")

                def open_image():
                    image.locator(".source-review-button").click()
                    expect(page.locator("#source-review-dialog")).to_be_visible()

                def save(kind, reason):
                    page.locator("#source-review-kind").select_option(kind)
                    page.locator("#source-reviewer").fill("材料核对验收员")
                    page.locator("#source-review-reason").fill(reason)
                    page.locator("#source-review-confirm").check()
                    page.locator("#save-source-review").click()
                    expect(page.locator("#source-review-dialog")).not_to_be_visible()
                    page.wait_for_function("() => !document.querySelector('#source-review-form').inert")

                open_image()
                with page.expect_download() as pending_download:
                    page.locator("#source-review-original a").click()
                download = pending_download.value
                original = output / "downloaded-banner.png"
                download.save_as(original)
                source_hash = page.evaluate("S.sourceReview.file.sha256")
                assert hashlib.sha256(original.read_bytes()).hexdigest() == source_hash
                save("non_billing_image", "已查看合成原图，确认无需要入账的费用。")
                expect(image.locator(".pill")).to_have_text("已确认·非账单图片")
                expect(page.locator("#processing-summary")).to_contain_text("0 个用途待确认")
                assert page.evaluate("S.result.job.status") == "completed"
                assert repository.snapshot(book)[1] == revision
                assert dumps(repository.snapshot(book)[0].model_dump(mode="python")) == dumps(before.model_dump(mode="python"))
                checks.append("download_nested_original_and_confirm_without_ledger_changes")

                page.reload()
                page.wait_for_function("() => S.config && S.ledgers.length > 0")
                page.evaluate("(id) => openJob(id)", job)
                expect(image.locator(".pill")).to_have_text("已确认·非账单图片")
                open_image()
                page.locator("#source-review-history").evaluate("element => element.open = true")
                expect(page.locator("#source-review-history-items p")).to_have_count(1)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert page.locator("#source-review-dialog").evaluate("element => element.scrollWidth <= element.clientWidth")
                page.screenshot(path=str(output / "confirmation-mobile.png"))
                save("reopen", "重新核对材料用途，恢复为待确认。")
                expect(image.locator(".pill")).to_have_text("用途待确认")
                expect(page.locator("#processing-summary")).to_contain_text("1 个用途待确认")
                open_image()
                expect(page.locator("#source-review-history-items p")).to_have_count(2)
                save("non_billing_image", "再次核对原图后确认无需录入费用。")
                expect(image.locator(".pill")).to_have_text("已确认·非账单图片")
                checks.append("persisted_history_reopen_reconfirm_and_mobile_layout")

                page.locator("#download-kind").select_option("source-reviews.json")
                with page.expect_download() as pending_export:
                    page.locator("#download-button").click()
                export = output / "source-reviews.json"
                pending_export.value.save_as(export)
                assert len(next(iter(load_json(export).values()))) == 3
                assert all((app.state.store.folder(job) / "result" / name).read_bytes() == data for name, data in frozen.items())
                assert repository.snapshot(book)[1] == revision
                assert not errors, errors
                checks.append("history_export_preserves_all_original_artifacts_and_ledger_revision")
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
