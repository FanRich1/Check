import pytest
from pydantic import ValidationError

from shipping_billing.config import Settings


def test_offline_has_no_credential_requirement():
    assert Settings().mode == "offline"
    assert Settings().public_summary()["network_call_performed"] is False


def test_dotenv_must_be_explicit(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("BILLING_MODE=online\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().mode == "offline"


def test_online_rejects_missing_credentials():
    with pytest.raises(ValidationError):
        Settings(mode="online")


def test_secret_never_in_public_summary_or_repr():
    s = Settings(mode="online", model_provider="test", model_name="test", model_api_key="CANARY-secret")
    assert "CANARY-secret" not in repr(s)
    assert "CANARY-secret" not in str(s.public_summary())


def test_explicit_dotenv(tmp_path):
    f = tmp_path / "test.env"
    f.write_text("BILLING_LOG_LEVEL=WARNING\n", encoding="utf-8")
    assert Settings(_env_file=f).log_level == "WARNING"
