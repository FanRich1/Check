"""Run database acceptance checks using a dedicated test database, never the application ledger."""

import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from shipping_billing.config import Settings


def main():
    root = Path(__file__).resolve().parents[1]
    settings = Settings(_env_file=root / ".env")
    if not settings.database_url:
        print("Configure BILLING_DATABASE_URL before running real MySQL checks.")
        return 1
    url = make_url(settings.database_url.get_secret_value())
    url = url.set(database="shipping_billing_test")
    os.environ["BILLING_TEST_MYSQL_URL"] = url.render_as_string(hide_password=False)
    return pytest.main(["tests/integration/test_database.py", "-q",
                        "--junitxml=outputs/mysql/acceptance.xml"])


if __name__ == "__main__":
    raise SystemExit(main())
