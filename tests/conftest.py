import os
from pathlib import Path

import pytest

from shipping_billing.io import load_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/synthetic"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in list(os.environ):
        if name.startswith("BILLING_"):
            monkeypatch.delenv(name)


@pytest.fixture
def base_record():
    return load_json(FIXTURES / "DEV-001/expected.json")["expected_records"][0]


@pytest.fixture
def base_request():
    return load_json(FIXTURES / "DEV-001/request.json")
