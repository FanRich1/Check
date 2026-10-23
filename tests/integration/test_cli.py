import subprocess
import sys
from pathlib import Path

from shipping_billing.cli import main
from shipping_billing.io import load_json, write_json

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/synthetic"


def test_installed_module_help():
    run = subprocess.run([sys.executable, "-m", "shipping_billing", "--help"], capture_output=True, text=True)
    assert run.returncode == 0
    assert "evaluate" in run.stdout


def test_normalize_cli_ready_and_missing_entity(tmp_path):
    from scripts.stage5_cases import make_input

    source, context = make_input()
    input_path, ctx_path, output = (
        tmp_path / "input.json",
        tmp_path / "context.json",
        tmp_path / "output.json",
    )
    write_json(input_path, source.model_dump(mode="python"))
    write_json(ctx_path, context.model_dump(mode="python"))
    assert main(["normalize", str(input_path), "--context", str(ctx_path), "--output", str(output)]) == 0
    line = load_json(output)["lines"][0]
    assert line["financial_attributes"]["posting_type"] == "AP" and line["status"] == "ready"
    assert main(["normalize", str(input_path), "--output", str(output)]) == 1
    assert load_json(output)["lines"][0]["financial_attributes"]["posting_type"] is None
    assert main(["normalize", str(input_path), "--context", str(ctx_path), "--output", str(ctx_path)]) == 2


def test_normalize_rejects_context_conflict_and_bad_input(tmp_path):
    from scripts.stage5_cases import make_input

    source, context = make_input()
    path, ctx, output = tmp_path / "input.json", tmp_path / "context.json", tmp_path / "output.json"
    write_json(path, source.model_dump(mode="python"))
    write_json(ctx, context.model_dump(mode="python"))
    assert (
        main(
            [
                "normalize",
                str(path),
                "--context",
                str(ctx),
                "--accounting-entity",
                "A",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    invalid = source.model_dump(mode="python")
    invalid["files"][0]["candidates"][0]["charge_details"]["amount"] = "DO_NOT_ECHO"
    write_json(path, invalid)
    assert main(["normalize", str(path), "--output", str(output)]) == 2


def test_normalize_source_error_is_not_successful_empty_output(tmp_path):
    from scripts.stage5_cases import make_input

    source, _ = make_input()
    source.files[0].status = "error"
    source.files[0].candidates = []
    path, output = tmp_path / "input.json", tmp_path / "output.json"
    write_json(path, source.model_dump(mode="python"))
    assert main(["normalize", str(path), "--output", str(output)]) == 3
    assert load_json(output)["files"][0]["status"] == "error"


def test_parse_office_and_partial_failure(tmp_path, capsys):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/parsing/fees.xlsx"
    report = tmp_path / "parsed.json"
    storage = tmp_path / "storage"
    assert main(["parse", str(fixture), "--storage", str(storage), "--output", str(report)]) == 0
    result = load_json(report)
    assert result["sources"][0]["tables"][1]["rows"][0][1] == 65
    assert result["billing_pipeline_executed"] is False
    assert (
        main(
            [
                "parse",
                str(tmp_path / "missing.pdf"),
                str(fixture),
                "--storage",
                str(storage),
                "--output",
                str(report),
            ]
        )
        == 3
    )
    assert len(load_json(report)["sources"]) == 2


def test_parse_protects_input_and_requires_delivery(tmp_path):
    fixture = Path(__file__).resolve().parents[1] / "fixtures/parsing/fees.xlsx"
    assert main(["parse", str(fixture), "--output", str(fixture)]) == 2
    assert main(["parse", str(fixture), "--mode", "live", "--output", str(tmp_path / "out.json")]) == 2


def test_extract_requires_online_config_and_protects_input(tmp_path):
    from shipping_billing.parsers.contracts import ParseReport

    path = tmp_path / "parsed.json"
    write_json(path, ParseReport(mode="offline").model_dump(mode="python"))
    assert main(["extract", str(path), "--mode", "live", "--output", str(tmp_path / "result.json")]) == 2
    assert main(["extract", str(path), "--mode", "replay", "--output", str(path)]) == 2


def test_extract_replay_failure_writes_complete_source_status(tmp_path):
    from shipping_billing.models import Locator
    from shipping_billing.parsers.contracts import ParsedSource, ParseReport

    source = ParsedSource(source_id="a", original_name="test.eml", sha256=None, size_bytes=None)
    source.fragment("Gate fee USD 10", Locator(kind="email_body", section="body"), "email_plain")
    path, out = tmp_path / "input.json", tmp_path / "output.json"
    write_json(path, ParseReport(mode="offline", sources=[source]).model_dump(mode="python"))
    code = main(
        [
            "extract",
            str(path),
            "--mode",
            "replay",
            "--storage",
            str(tmp_path / "archives"),
            "--output",
            str(out),
        ]
    )
    assert code == 3
    report = load_json(out)
    assert report["model_invocations"] == 0 and len(report["files"]) == 1
    assert report["files"][0]["status"] == "error"
    assert report["billing_pipeline_executed"] is False


def test_validate_good_request(capsys):
    assert main(["validate", "request", str(FIXTURES / "DEV-001/request.json")]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_validate_bad_records_reports_safe_error(tmp_path, capsys):
    p = tmp_path / "bad.json"
    write_json(p, [{"operation": "CANARY-secret"}])
    assert main(["validate", "records", str(p)]) == 2
    captured = capsys.readouterr()
    assert "CANARY-secret" not in captured.err
    assert "VALIDATION_FAILED" in captured.err


def test_online_error_does_not_expose_secret(monkeypatch, capsys):
    monkeypatch.setenv("BILLING_MODEL_API_KEY", "CANARY-secret")
    assert main(["config-check", "--mode", "online"]) == 2
    assert "CANARY-secret" not in capsys.readouterr().err


def test_pipeline_requires_inputs_and_output(capsys):
    import pytest

    with pytest.raises(SystemExit) as error:
        main(["run"])
    assert error.value.code == 2
    assert "required" in capsys.readouterr().err


def test_schema_export(tmp_path):
    from jsonschema import Draft202012Validator

    p = tmp_path / "schema.json"
    assert main(["schema", "records", "--output", str(p)]) == 0
    Draft202012Validator.check_schema(load_json(p))


def test_evaluation_pass_and_fail(tmp_path):
    case = FIXTURES / "DEV-001"
    predictions = load_json(case / "expected.json")["expected_records"]
    actual = tmp_path / "actual.json"
    report = tmp_path / "report.json"
    write_json(actual, predictions)
    args = ["evaluate", "--case", str(case), "--actual", str(actual), "--report", str(report)]
    assert main(args) == 0
    assert load_json(report)["exact_business_records"]["numerator"] == 1
    assert load_json(report)["pipeline_executed"] is False
    predictions[0]["charge_details"]["amount"] = 41
    write_json(actual, predictions)
    assert main(args) == 1
    result = load_json(report)
    assert result["exact_business_records"]["numerator"] == 0
    assert result["differences"][0]["different_groups"] == ["charge_details"]


def test_label_file_not_a_prediction(tmp_path):
    case = FIXTURES / "DEV-001"
    assert (
        main(
            [
                "evaluate",
                "--case",
                str(case),
                "--actual",
                str(case / "expected.json"),
                "--report",
                str(tmp_path / "report.json"),
            ]
        )
        == 2
    )


def test_missing_prediction_row_remains_in_denominator(tmp_path):
    case = FIXTURES / "DEV-001"
    actual, report = tmp_path / "actual.json", tmp_path / "report.json"
    write_json(actual, [])
    assert main(["evaluate", "--case", str(case), "--actual", str(actual), "--report", str(report)]) == 1
    assert load_json(report)["exact_business_records"] == {"numerator": 0, "denominator": 1, "rate": 0}


def test_invalid_prediction_generates_failed_report_without_dropping_case(tmp_path):
    case = FIXTURES / "DEV-001"
    actual, report = tmp_path / "actual.json", tmp_path / "report.json"
    write_json(actual, [{"operation": "CANARY-secret"}])
    assert main(["evaluate", "--case", str(case), "--actual", str(actual), "--report", str(report)]) == 1
    result = load_json(report)
    assert result["prediction_contract_valid"] is False
    assert result["exact_business_records"]["denominator"] == 1
    assert "CANARY-secret" not in report.read_text(encoding="utf-8")


def test_env_file_cannot_be_overwritten_by_report(tmp_path):
    env = tmp_path / "private.env"
    env.write_text("BILLING_MODE=offline\n", encoding="utf-8")
    before = env.read_bytes()
    assert main(["--env-file", str(env), "probe-model", "--report", str(env)]) == 2
    assert env.read_bytes() == before


def test_local_ocr_requires_download_mapping_before_any_network(tmp_path):
    local = tmp_path / "bill.pdf"
    local.write_bytes(b"hash-only fixture")
    assert (
        main(["ocr", "--local-file", str(local), "--source-id", "s", "--report", str(tmp_path / "r.json")])
        == 2
    )


def test_inputs_cannot_be_overwritten():
    path = FIXTURES / "DEV-001/request.json"
    before = path.read_bytes()
    assert main(["validate", "request", str(path), "--output", str(path)]) == 2
    assert path.read_bytes() == before


def test_missing_explicit_environment_file(tmp_path):
    assert main(["--env-file", str(tmp_path / "missing.env"), "config-check"]) == 2


def test_io_write_failure_has_nonzero_exit(tmp_path):
    # A regular file cannot be the parent of an output directory.
    parent = tmp_path / "file"
    parent.write_text("x", encoding="utf-8")
    assert main(["schema", "records", "--output", str(parent / "schema.json")]) == 3
