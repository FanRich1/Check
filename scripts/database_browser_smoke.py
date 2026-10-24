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
            page = browser.new_page(viewport={"width": 1440, "height": 1100}, accept_downloads=True)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#database-ledger-fields")).to_be_visible()
            expect(page.locator("#legacy-ledger-input")).to_be_hidden()
            page.locator("#new-ledger-button").click()
            page.locator("#new-ledger-name").fill("浏览器验收 " + uuid4().hex[:8])
            page.locator("#new-ledger-entity").fill("Buyer Ltd")
            page.locator('#create-ledger-form button[type="submit"]').click()
            expect(page.locator("#managed-ledger-summary")).to_contain_text("Buyer Ltd")
            page.locator("#close-ledger").click()
            expect(page.locator("#entity")).to_have_value("Buyer Ltd")
            assert page.locator("#entity").evaluate("e => e.readOnly")
            page.screenshot(path=str(output / "workspace.png"), full_page=True)
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
            process(raw, "无变化")
            page.locator("#continue-button").click()
            process(eml(output / "amend.eml", 50, note="Update amount from 40 to 50"), "修改")
            expect(page.locator("#record-rows .amount")).to_have_text("50")
            checks.append("upload_repeat_authorized_update_automatic_ledger_matching")
            page.screenshot(path=str(output / "updated.png"), full_page=True)
            page.locator("#continue-button").click()
            page.locator("#manage-ledger-button").click()
            expect(page.locator("#ledger-bills")).to_contain_text("50 USD")
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
            expect(page.locator("#managed-ledger-summary")).to_contain_text("版本 0")
            imported = load_json(ROOT / "tests/fixtures/reconciliation/example-old-state.json")
            imported["bills"][0]["charge_details"]["amount"] = Decimal("9007199254740993.12")
            old_path = output / "import.json"
            write_json(old_path, imported)
            page.locator("#import-ledger-file").set_input_files(str(old_path))
            page.locator("#import-ledger-button").click()
            expect(page.locator("#ledger-bills")).to_contain_text("9007199254740993.12 USD")
            with page.expect_download() as download:
                page.locator("#export-ledger-button").click()
            download.value.save_as(output / "large-export.json")
            assert dumps(load_json(output / "large-export.json")) == dumps(imported)
            page.screenshot(path=str(output / "ledger-import.png"), full_page=True)
            page.locator("#close-ledger").click()
            checks.append("import_old_ledger_large_decimal_extensions_exact_download")
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
