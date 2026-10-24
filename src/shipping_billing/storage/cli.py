"""Explicit MySQL ledger administration without printing connection secrets."""

import argparse
from pathlib import Path

from pydantic import ValidationError

from shipping_billing.config import Settings
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps, load_json, write_json
from shipping_billing.reconciliation.contracts import LedgerState
from shipping_billing.storage.database import LedgerRepository


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("list")
    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--entity", required=True)
    old = sub.add_parser("import")
    old.add_argument("--ledger", required=True)
    old.add_argument("--state", type=Path, required=True)
    old.add_argument("--revision", type=int, default=0)
    export = sub.add_parser("export")
    export.add_argument("--ledger", required=True)
    export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    repository = None
    try:
        settings = Settings(_env_file=args.env_file)
        if not settings.database_url:
            raise BillingError("DATABASE_NOT_CONFIGURED", "请先配置 BILLING_DATABASE_URL。")
        repository = LedgerRepository(settings.database_url.get_secret_value())
        if args.command == "init":
            result = {"backend": repository.backend, "schema_version": 1, "ready": True}
        elif args.command == "list":
            result = repository.ledgers()
        elif args.command == "create":
            result = repository.create_ledger(args.name, args.entity)
        elif args.command == "import":
            value = load_json(args.state)
            state = LedgerState(bills=value) if isinstance(value, list) else LedgerState.model_validate(value)
            result = repository.import_state(args.ledger, state, args.revision)
        else:
            if args.output.resolve() == args.env_file.resolve():
                raise BillingError("OUTPUT_COLLISION", "导出路径不能覆盖配置。")
            state, revision = repository.snapshot(args.ledger)
            write_json(args.output, state.model_dump(mode="python"))
            result = {"ledger_id": args.ledger, "revision": revision, "bills": len(state.bills)}
        print(dumps(result))
        return 0
    except BillingError as exc:
        print(dumps({"error": {"code": exc.code, "message": exc.message}}))
        return 2
    except ValidationError:
        print(dumps({"error": {"code": "INVALID_CONFIGURATION_OR_STATE", "message": "配置或旧账本无效。"}}))
        return 2
    finally:
        if repository:
            repository.close()


if __name__ == "__main__":
    raise SystemExit(main())
