"""Real Edge UI checks. Install Playwright only for QA; --live explicitly enables synthetic model calls."""

import argparse
from pathlib import Path

from shipping_billing.io import load_json, write_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright

    output = ROOT / "outputs/web/qa"
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url)
        expect(page.locator("#demo-button")).to_be_enabled()
        page.screenshot(path=str(output / "home.png"), full_page=True)
        page.locator("#nav-help").click()
        expect(page.locator("#help-dialog")).to_be_visible()
        page.locator("#close-help").click()
        page.locator("#document-input").set_input_files(
            {
                "name": "<img src=x onerror=window.__xss=1>.eml",
                "mimeType": "message/rfc822",
                "buffer": b"From: x@example.invalid\r\n\r\nraw content",
            }
        )
        expect(page.locator("#selected-files img")).to_have_count(0)
        assert page.evaluate("window.__xss === undefined")
        page.locator("#entity").fill("Buyer Ltd")
        blocked_posts = []

        def disconnected_upload(route):
            if route.request.method == "POST":
                blocked_posts.append(route.request.url)
                route.abort("connectionrefused")
            else:
                route.continue_()

        page.route("**/api/jobs", disconnected_upload)
        page.locator("#submit-button").click()
        expect(page.locator("#form-error")).to_contain_text("无法连接本地工作台")
        expect(page.locator("#form-error")).to_contain_text("避免重复处理")
        expect(page.locator("#selected-files .selected-file")).to_have_count(1)
        expect(page.locator("#submit-button")).to_be_enabled()
        page.wait_for_timeout(500)
        assert len(blocked_posts) == 1, "A disconnected upload must not be automatically retried"
        page.unroute("**/api/jobs", disconnected_upload)
        checks.append("disconnected_upload_has_actionable_message_preserves_files_and_does_not_retry")
        page.locator(".remove-file").click()
        checks.append("help_and_safe_filename_rendering")
        page.locator("#demo-button").click()
        expect(page.locator("#result-content")).to_be_visible(timeout=30000)
        expect(page.locator("#record-rows tr")).to_have_count(2)
        page.locator("#record-search").fill("Workbook A")
        expect(page.locator("#record-rows tr")).to_have_count(1)
        page.locator("#record-search").fill("")
        page.locator('[data-operation="PENDING_REVIEW"]').click()
        expect(page.locator("#records-empty")).to_be_visible()
        page.locator('[data-operation="all"]').click()
        page.locator("#record-rows button").first.click()
        expect(page.locator("#detail-dialog")).to_be_visible()
        assert page.locator(".evidence-item blockquote").count() > 0
        page.locator("#review-status").select_option("checked")
        page.locator("#review-note").fill("Browser QA: checked source evidence.")
        page.locator('#review-form button[type="submit"]').click()
        expect(page.locator("#detail-dialog")).not_to_be_visible()
        expect(page.locator(".review-mark.checked")).to_have_count(1)
        with page.expect_download() as downloaded:
            page.locator("#download-button").click()
        downloaded.value.save_as(str(output / "downloaded-records.json"))
        assert len(load_json(output / "downloaded-records.json")) == 2
        page.screenshot(path=str(output / "results.png"), full_page=True)
        page.reload()
        page.locator("#nav-history").click()
        expect(page.locator("#history-jobs .job-card").first).to_be_visible()
        page.locator("#history-jobs .job-card").first.click()
        expect(page.locator("#record-rows tr")).to_have_count(2)
        expect(page.locator(".review-mark.checked")).to_have_count(1)
        checks.append("archived_workbook_filter_evidence_review_download_and_history")

        if args.live:
            page.locator("#back-workspace").click()
            page.locator("#entity").fill("Buyer Ltd")
            path = ROOT / "tests/fixtures/pipeline/inputs/new.eml"
            page.locator("#document-input").set_input_files(str(path))
            page.locator("#submit-button").click()
            expect(page.locator("#result-content")).to_be_visible(timeout=180000)
            expect(page.locator("#record-rows .pill.CREATE")).to_have_count(1)
            with page.expect_download() as download:
                page.locator("#download-kind").select_option("manifest.json")
                page.locator("#download-button").click()
            download.value.save_as(str(output / "live-manifest.json"))
            manifest = load_json(output / "live-manifest.json")
            assert manifest["mode"] == "live" and manifest["model_invocations"] > 0
            checks.append("real_model_eml_upload")
            page.locator("#continue-button").click()
            expect(page.locator("#base-job-banner")).to_be_visible()
            page.locator("#document-input").set_input_files(str(path))
            page.locator("#submit-button").click()
            expect(page.locator("#result-content")).to_be_visible(timeout=180000)
            expect(page.locator("#record-rows .pill.NO_CHANGE")).to_have_count(1)
            expect(page.locator("#record-rows .pill.CREATE")).to_have_count(0)
            checks.append("real_model_ledger_continuation_without_duplicate_create")
            page.screenshot(path=str(output / "live-result.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
            "Global horizontal overflow on mobile"
        )
        page.screenshot(path=str(output / "mobile-result.png"), full_page=True)
        page.locator("#nav-new").click()
        expect(page.locator("#drop-zone")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(output / "mobile-home.png"), full_page=True)
        checks.append("mobile_390px_no_global_horizontal_overflow")
        assert not errors, errors
        browser.close()
    write_json(
        output / ("browser-live-summary.json" if args.live else "browser-summary.json"),
        dict(passed=True, browser="Microsoft Edge", live=args.live, checks=checks, javascript_errors=errors),
    )
    print(f"Browser checks passed: {len(checks)}; JS errors: {len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
