"""Relational current balances, source identities and atomic per-job commit receipts."""

import hashlib
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import simplejson
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import TypeDecorator

from shipping_billing.domain.normalization import party_key
from shipping_billing.errors import BillingError
from shipping_billing.io import dumps
from shipping_billing.reconciliation.contracts import LedgerState

metadata = MetaData()
PAYLOAD = Text().with_variant(LONGTEXT(), "mysql")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def entity_key(value):
    return digest(" ".join(value.split()).casefold())


def loads(value):
    return simplejson.loads(value, use_decimal=True, allow_nan=False)


def amount_value(value):
    if value is None:
        return None
    amount = Decimal(value)
    if not amount.is_finite():
        raise BillingError("DATABASE_AMOUNT_RANGE", "金额必须是有限数值，未写入账本。")
    integer = max(amount.adjusted() + 1, 0) if amount else 0
    fraction = max(-amount.as_tuple().exponent, 0)
    if integer > 35 or fraction > 30:
        raise BillingError("DATABASE_AMOUNT_RANGE", "金额超出数据库精确存储范围，未写入账本。")
    return amount


class ExactAmount(TypeDecorator):
    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Text() if dialect.name == "sqlite" else Numeric(65, 30))

    def process_bind_param(self, value, dialect):
        value = amount_value(value)
        return str(value) if value is not None and dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return Decimal(value) if value is not None else None


versions = Table("billing_schema", metadata, Column("version", Integer, primary_key=True))
books = Table(
    "billing_ledgers", metadata,
    Column("id", String(32), primary_key=True), Column("name", String(200), nullable=False),
    Column("entity", String(300), nullable=False), Column("entity_key", String(64), nullable=False, index=True),
    Column("revision", BigInteger, nullable=False), Column("created_at", String(40), nullable=False),
    Column("audit_log", PAYLOAD, nullable=False),
)
bills = Table(
    "billing_bills", metadata,
    Column("ledger_id", String(32), ForeignKey("billing_ledgers.id"), nullable=False),
    Column("bill_key", String(64), nullable=False), Column("record_id", PAYLOAD, nullable=False),
    Column("position", Integer, nullable=False), Column("reference_no", String(191)),
    Column("charge_item", String(191)), Column("currency", String(3)), Column("amount", ExactAmount()),
    Column("mbl_no", String(191)), Column("booking_no", String(191)),
    Column("payload", PAYLOAD, nullable=False), PrimaryKeyConstraint("ledger_id", "bill_key"),
    Index("ix_billing_reference_fee", "ledger_id", "reference_no", "charge_item"),
    Index("ix_billing_mbl_currency", "ledger_id", "mbl_no", "currency"),
)
identities = Table(
    "billing_identities", metadata,
    Column("ledger_id", String(32), ForeignKey("billing_ledgers.id"), nullable=False),
    Column("occurrence_hash", String(64), nullable=False), Column("occurrence", PAYLOAD, nullable=False),
    Column("record_id", PAYLOAD, nullable=False), Column("position", Integer, nullable=False),
    PrimaryKeyConstraint("ledger_id", "occurrence_hash"),
)
jobs = Table(
    "billing_jobs", metadata, Column("id", String(32), primary_key=True),
    Column("ledger_id", String(32), ForeignKey("billing_ledgers.id")),
    Column("created_at", String(40), nullable=False), Column("status", String(32), nullable=False),
    Column("payload", PAYLOAD, nullable=False),
)
observations = Table(
    "billing_observations", metadata,
    Column("job_id", String(32), ForeignKey("billing_jobs.id"), nullable=False),
    Column("record_key", String(64), nullable=False), Column("operation", String(24), nullable=False),
    Column("payload", PAYLOAD, nullable=False), PrimaryKeyConstraint("job_id", "record_key"),
)
documents = Table(
    "billing_documents", metadata,
    Column("job_id", String(32), ForeignKey("billing_jobs.id"), nullable=False),
    Column("source_key", String(64), nullable=False), Column("sha256", String(64), index=True),
    Column("payload", PAYLOAD, nullable=False), PrimaryKeyConstraint("job_id", "source_key"),
)
reviews = Table(
    "billing_reviews", metadata,
    Column("job_id", String(32), ForeignKey("billing_jobs.id"), nullable=False),
    Column("record_key", String(64), nullable=False), Column("record_id", PAYLOAD, nullable=False),
    Column("payload", PAYLOAD, nullable=False), PrimaryKeyConstraint("job_id", "record_key"),
)
review_decisions = Table(
    "billing_review_decisions", metadata,
    Column("job_id", String(32), ForeignKey("billing_jobs.id"), nullable=False),
    Column("record_key", String(64), nullable=False), Column("record_id", PAYLOAD, nullable=False),
    Column("ledger_id", String(32), ForeignKey("billing_ledgers.id"), nullable=False),
    Column("payload", PAYLOAD, nullable=False), PrimaryKeyConstraint("job_id", "record_key"),
)
snapshots = Table(
    "billing_revisions", metadata,
    Column("ledger_id", String(32), ForeignKey("billing_ledgers.id"), nullable=False),
    Column("revision", BigInteger, nullable=False), Column("job_id", String(32)),
    Column("state_sha256", String(64), nullable=False), Column("payload", PAYLOAD, nullable=False),
    PrimaryKeyConstraint("ledger_id", "revision"),
)


class LedgerRepository:
    def __init__(self, url, *, allow_sqlite=False):
        try:
            parsed = make_url(url)
            if parsed.drivername != "mysql+pymysql" and not (
                allow_sqlite and parsed.drivername == "sqlite+pysqlite"
            ):
                raise BillingError("DATABASE_CONFIG_INVALID", "数据库连接须使用 mysql+pymysql。")
            self.backend = parsed.get_backend_name()
            kwargs = dict(pool_pre_ping=True, hide_parameters=True)
            if self.backend == "mysql":
                kwargs.update(connect_args={"connect_timeout": 5}, pool_recycle=1800)
            self.engine = create_engine(parsed, **kwargs)
            with self.transaction() as connection:
                metadata.create_all(connection)
                stored = connection.execute(select(versions.c.version)).scalars().all()
                if stored and stored != [1]:
                    raise BillingError("DATABASE_SCHEMA_VERSION", "数据库结构版本不兼容，请先执行迁移。")
                if not stored:
                    connection.execute(insert(versions).values(version=1))
        except BillingError:
            raise
        except (SQLAlchemyError, ValueError, ImportError):
            raise BillingError("DATABASE_UNAVAILABLE", "无法连接数据库，请检查本机 MySQL 和连接配置。") from None

    @contextmanager
    def transaction(self):
        try:
            with self.engine.begin() as connection:
                yield connection
        except SQLAlchemyError:
            raise BillingError("DATABASE_TRANSACTION_FAILED", "数据库事务未完成，账本未确认更新，请检查服务状态。") from None

    def close(self):
        self.engine.dispose()

    def _book(self, connection, ledger_id, lock=False):
        query = select(books).where(books.c.id == ledger_id)
        if lock:
            query = query.with_for_update()
        row = connection.execute(query).mappings().first()
        if row is None:
            raise BillingError("LEDGER_NOT_FOUND", "账本不存在，请重新选择。")
        return dict(row)

    def create_ledger(self, name, entity):
        name, entity = name.strip(), entity.strip()
        if not name or len(name) > 200 or not entity or len(entity) > 300:
            raise BillingError("INVALID_LEDGER", "请填写账本名称和记账公司全称。")
        item = dict(id=uuid4().hex, name=name, entity=entity, entity_key=entity_key(entity),
                    revision=0, created_at=stamp(), audit_log="[]")
        with self.transaction() as connection:
            connection.execute(insert(books).values(**item))
        return self.ledger(item["id"])

    def ledgers(self):
        with self.transaction() as connection:
            rows = connection.execute(select(books).order_by(books.c.created_at)).mappings().all()
            counts = dict(connection.execute(
                select(bills.c.ledger_id, func.count()).group_by(bills.c.ledger_id)
            ).all())
            return [{k: row[k] for k in ("id", "name", "entity", "revision", "created_at")}
                    | {"bill_count": counts.get(row["id"], 0)} for row in rows]

    def ledger(self, ledger_id):
        return next((b for b in self.ledgers() if b["id"] == ledger_id), None) or self._missing()

    def _missing(self):
        raise BillingError("LEDGER_NOT_FOUND", "账本不存在，请重新选择。")

    def snapshot(self, ledger_id):
        with self.transaction() as connection:
            book = self._book(connection, ledger_id, lock=True)
            return self._snapshot(connection, book), book["revision"]

    def _snapshot(self, connection, book):
        ledger_id = book["id"]
        rows = connection.execute(select(bills).where(bills.c.ledger_id == ledger_id)
                                  .order_by(bills.c.position)).mappings().all()
        keys = connection.execute(select(identities).where(identities.c.ledger_id == ledger_id)
                                  .order_by(identities.c.position)).mappings().all()
        grouped = {}
        for row in keys:
            grouped.setdefault(row["record_id"], []).append(row["occurrence"])
        archived = connection.execute(select(snapshots.c.payload).where(
            (snapshots.c.ledger_id == ledger_id) & (snapshots.c.revision == book["revision"])
        )).scalar_one_or_none()
        return LedgerState.model_validate(dict(
            bills=[loads(r["payload"]) for r in rows],
            identities=loads(archived)["identities"] if archived else [
                dict(record_id=k, occurrence_keys=v) for k, v in grouped.items()
            ],
            audit_log=loads(book["audit_log"]),
        ))

    def _validate_state(self, state, book):
        for bill in state.bills:
            if bill.model_dump().get("operation") == "PENDING_REVIEW":
                raise BillingError("PENDING_BILL_IMPORT", "待复核结果不能导入为正式账单，请导入有效账本。")
            if bill.charge_details.amount is None or not bill.charge_details.currency or not bill.charge_details.charge_item or (
                bill.financial_attributes.posting_type not in ("AR", "AP")
            ):
                raise BillingError("INVALID_LEDGER_BILL", "正式账单缺少金额、币种或明确应收应付，未写入。")
            if party_key(bill.financial_attributes.accounting_entity or "") != party_key(book["entity"]):
                raise BillingError("LEDGER_ENTITY_CONFLICT", "账单的记账主体与所选账本不一致，未写入。")
            amount_value(bill.charge_details.amount)

    def _replace(self, connection, book, state, job_id):
        ledger_id = book["id"]
        connection.execute(delete(identities).where(identities.c.ledger_id == ledger_id))
        connection.execute(delete(bills).where(bills.c.ledger_id == ledger_id))
        for position, bill in enumerate(state.bills):
            payload = bill.model_dump(mode="python", exclude_unset=True)
            ship = payload.get("shipping_info", {})
            def short(value):
                return str(value)[:191] if value is not None else None
            connection.execute(insert(bills).values(
                ledger_id=ledger_id, bill_key=digest(bill.record_id), record_id=bill.record_id,
                position=position, reference_no=short(bill.document_info.reference_no),
                charge_item=short(bill.charge_details.charge_item), currency=bill.charge_details.currency,
                amount=bill.charge_details.amount, mbl_no=short(ship.get("mbl_no")),
                booking_no=short(ship.get("booking_no")), payload=dumps(payload),
            ))
        position = 0
        for identity in state.identities:
            for key in identity.occurrence_keys:
                connection.execute(insert(identities).values(
                    ledger_id=ledger_id, occurrence_hash=digest(key), occurrence=key,
                    record_id=identity.record_id, position=position,
                ))
                position += 1
        revision = book["revision"] + 1
        encoded = dumps(state.model_dump(mode="python"))
        connection.execute(update(books).where(books.c.id == ledger_id).values(
            revision=revision, audit_log=dumps(state.audit_log)))
        connection.execute(insert(snapshots).values(
            ledger_id=ledger_id, revision=revision, job_id=job_id,
            state_sha256=digest(encoded), payload=encoded))
        return revision

    def import_state(self, ledger_id, state, expected_revision):
        with self.transaction() as connection:
            book = self._book(connection, ledger_id, lock=True)
            if book["revision"] != expected_revision:
                raise BillingError("LEDGER_CHANGED", "账本已更新，请刷新后重试。")
            if book["revision"] != 0:
                raise BillingError("LEDGER_NOT_EMPTY", "旧账单仅可导入新建空账本，已有账本请通过材料匹配更新。")
            self._validate_state(state, book)
            self._replace(connection, book, state, None)
        return self.ledger(ledger_id)

    def _save_job(self, connection, item):
        values = dict(ledger_id=item.get("ledger_id"), created_at=item["created_at"],
                      status=item["status"], payload=dumps(item))
        exists = connection.execute(select(jobs.c.id).where(jobs.c.id == item["id"])).first()
        if exists:
            connection.execute(update(jobs).where(jobs.c.id == item["id"]).values(**values))
        else:
            connection.execute(insert(jobs).values(id=item["id"], **values))

    def save_job(self, item):
        with self.transaction() as connection:
            self._save_job(connection, item)

    def get_job(self, job_id):
        with self.transaction() as connection:
            value = connection.execute(select(jobs.c.payload).where(jobs.c.id == job_id)).scalar_one_or_none()
            if value is None:
                raise BillingError("JOB_NOT_FOUND", "找不到该任务。")
            return loads(value)

    def list_jobs(self):
        with self.transaction() as connection:
            return [loads(row) for row in connection.execute(
                select(jobs.c.payload).order_by(jobs.c.created_at.desc()).limit(100)).scalars()]

    def _results(self, connection, job_id, records, files):
        connection.execute(delete(observations).where(observations.c.job_id == job_id))
        connection.execute(delete(documents).where(documents.c.job_id == job_id))
        for record in records:
            connection.execute(insert(observations).values(
                job_id=job_id, record_key=digest(record["record_id"]),
                operation=record["operation"], payload=dumps(record)))
        for file in files:
            connection.execute(insert(documents).values(
                job_id=job_id, source_key=digest(file["source_id"]),
                sha256=file.get("sha256"), payload=dumps(file)))

    def commit_job(self, item, state, records, files):
        with self.transaction() as connection:
            book = self._book(connection, item["ledger_id"], lock=True)
            previous = connection.execute(select(jobs.c.payload).where(jobs.c.id == item["id"])).scalar_one()
            previous = loads(previous)
            if previous.get("ledger_commit_status") == "committed":
                return previous
            if book["revision"] != item["ledger_base_revision"]:
                raise BillingError("LEDGER_CHANGED", "账本已被其他任务更新，本次结果未入库，请重新处理。")
            self._validate_state(state, book)
            revision = self._replace(connection, book, state, item["id"])
            item = {**item, "ledger_commit_status": "committed", "ledger_revision": revision,
                    "ledger_bill_count": len(state.bills)}
            self._save_job(connection, item)
            self._results(connection, item["id"], records, files)
            return item

    def save_results(self, item, records, files):
        with self.transaction() as connection:
            self._save_job(connection, item)
            self._results(connection, item["id"], records, files)

    def save_review(self, job_id, record_id, item):
        values = dict(record_id=record_id, payload=dumps(item))
        with self.transaction() as connection:
            key = digest(record_id)
            condition = (reviews.c.job_id == job_id) & (reviews.c.record_key == key)
            if connection.execute(select(reviews.c.record_key).where(condition)).first():
                connection.execute(update(reviews).where(condition).values(**values))
            else:
                connection.execute(insert(reviews).values(job_id=job_id, record_key=key, **values))

    def get_reviews(self, job_id):
        with self.transaction() as connection:
            return {r.record_id: loads(r.payload) for r in connection.execute(
                select(reviews.c.record_id, reviews.c.payload).where(reviews.c.job_id == job_id))}

    def get_decisions(self, job_id):
        with self.transaction() as connection:
            return {r.record_id: loads(r.payload) for r in connection.execute(
                select(review_decisions.c.record_id, review_decisions.c.payload)
                .where(review_decisions.c.job_id == job_id))}

    def _review_job(self, connection, origin, ledger_id):
        value = connection.execute(select(jobs.c.payload).where(jobs.c.id == origin["job_id"])
                                   .with_for_update()).scalar_one_or_none()
        if value is None:
            raise BillingError("JOB_NOT_FOUND", "找不到该任务。")
        item = loads(value)
        if item.get("demo") or item["status"] in ("queued", "running", "failed", "interrupted"):
            raise BillingError("REVIEW_NOT_AVAILABLE", "示例、未完成或中断任务不能正式复审入账。")
        if item.get("ledger_id") and item.get("ledger_commit_status") != "committed":
            raise BillingError("REVIEW_NOT_AVAILABLE", "本次任务账本提交未完成，请重新处理材料后复审。")
        if (item.get("ledger_id") or item.get("review_ledger_id") or ledger_id) != ledger_id:
            raise BillingError("LEDGER_ENTITY_CONFLICT", "请使用该任务原本所属的账本。")
        return item

    def preview_review(self, origin, request):
        from shipping_billing.storage.review import build_plan, visible_plan

        with self.transaction() as connection:
            book = self._book(connection, request.ledger_id, lock=True)
            self._review_job(connection, origin, book["id"])
            if connection.execute(select(review_decisions.c.record_key).where(
                (review_decisions.c.job_id == origin["job_id"])
                & (review_decisions.c.record_key == digest(request.record_id))
            )).first():
                raise BillingError("REVIEW_ALREADY_RESOLVED", "该费用已完成正式复审，请刷新查看记录。")
            plan, state = build_plan(self._snapshot(connection, book), book, origin, request)
            self._validate_state(state, book)
            return visible_plan(plan)

    def approve_review(self, origin, request, preview_token):
        from shipping_billing.storage.review import build_plan, request_hash, visible_plan

        with self.transaction() as connection:
            book = self._book(connection, request.ledger_id, lock=True)
            item = self._review_job(connection, origin, book["id"])
            condition = ((review_decisions.c.job_id == origin["job_id"])
                         & (review_decisions.c.record_key == digest(request.record_id)))
            existing = connection.execute(select(review_decisions.c.payload).where(condition)).scalar_one_or_none()
            if existing:
                receipt = loads(existing)
                if receipt["request_sha256"] == request_hash(request) and secrets.compare_digest(
                    receipt["preview_token"], preview_token
                ):
                    return visible_plan(receipt)
                raise BillingError("REVIEW_ALREADY_RESOLVED", "该费用已有正式决定，不能再次入账或覆盖决定。")
            plan, state = build_plan(self._snapshot(connection, book), book, origin, request)
            if not secrets.compare_digest(plan["preview_token"], preview_token):
                raise BillingError("REVIEW_PREVIEW_CHANGED", "复审内容已变化，请重新预览再提交。")
            receipt = {**plan, "decision_id": uuid4().hex, "decided_at": stamp(),
                       "ledger_revision": book["revision"] + 1}
            state.audit_log.append({"kind": "human_review", **receipt})
            self._validate_state(state, book)
            self._replace(connection, book, state, item["id"])
            connection.execute(insert(review_decisions).values(
                job_id=item["id"], record_key=digest(request.record_id), record_id=request.record_id,
                ledger_id=book["id"], payload=dumps(receipt)))
            item["review_ledger_id"] = book["id"]
            resolved = connection.scalar(select(func.count()).select_from(review_decisions)
                                         .where(review_decisions.c.job_id == item["id"]))
            pending = connection.scalar(select(func.count()).select_from(observations).where(
                (observations.c.job_id == item["id"]) & (observations.c.operation == "PENDING_REVIEW")))
            item.update(review_resolved_count=resolved, review_pending_count=max(pending - resolved, 0),
                        review_latest_revision=receipt["ledger_revision"], review_bill_count=len(state.bills))
            self._save_job(connection, item)
            return visible_plan(receipt)
